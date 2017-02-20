#!/usr/bin/python3
# driver to process chunks of samples using CUDA
# uses shared memory to get samples from usrp_drivers 
# spawn one cude driver per computer
# sends processed samples to usrp_server using sockets
# requires python 3 (need mmap with buffer interface to move samples directly between shared memory and gpu)

import socket
import pdb
import numpy as np
import scipy.special
import mmap
import os
import sys
import argparse
import signal
import functools 
import configparser
import logging

import posix_ipc
import pycuda.driver as cuda
import pycuda.compiler
import pycuda.autoinit

import pickle # for cuda dump
from datetime import datetime

sys.path.insert(0, '../python_include')

from socket_utils import *
from drivermsg_library import *
import dsp_filters
from phasing_utils import *
import logging_usrp

# import pycuda stuff
SWING0 = 0
SWING1 = 1
allSwings = [SWING0, SWING1]
nSwings = len(allSwings)


SIDEA = 0
SIDEB = 1

RXDIR = 'rx'
TXDIR = 'tx'

DEBUG = True
verbose = 1
C = 3e8



rx_shm_list = [[ [] for iSwing in allSwings] for iSide in range(2)]
tx_shm_list = [ [] for iSwing in allSwings]  # so far same tx data for both sides
rx_sem_list = [[ [] for iSwing in allSwings] for iSide in range(2)]
tx_sem_list = [[ [] for iSwing in allSwings] for iSide in range(2)] # side is hard coded to SIDEA 


swings = [SWING0, SWING1]
sides = [SIDEA]

# list of all semaphores/shared memory paths for cleaning up
shm_list = []
sem_list = []



# TODO: write decorator for acquring/releasing semaphores

# python3 or greater is needed for direct transfers between shm and gpu memory
if sys.hexversion < 0x030300F0:
    loggin.error('this code requires python 3.3 or greater')
    sys.exit(0)


class cudamsg_handler(object):
    def __init__(self, serversock, command, gpu, antennas, array_info, hardware_limits):
        self.sock = serversock
        self.antennas = np.uint16(antennas)
        self.command = command
        self.status = 0
        self.gpu = gpu
        self.array_info = array_info
        self.hardware_limits = hardware_limits
        self.logger = logging.getLogger('msg_handler')

    def respond(self):
        transmit_dtype(self.sock, self.command, np.uint8)

    def process(self):
        raise NotImplementedError('The process method for this driver message is unimplemented')

# take copy and process data from shared memory, send to usrp_server via socks 
class cuda_generate_pulse_handler(cudamsg_handler):
    def process(self):
        self.logger.debug('enter cuda_generate_pulse_handler.process')

        cmd = cuda_generate_pulse_command([self.sock])
        cmd.receive(self.sock)
        swing = cmd.payload['swing']
 
        if not any(self.gpu.sequences[swing]):
            self.logger.error("no sequences are defined. Pulse generation not possible")
            return 

        # create empty baseband transmit waveform vector
        nPulses   = self.gpu.nPulses
        nAntennas = self.gpu.nAntennas
        nChannels = self.gpu.nChannels
       
        # generate base band signals incl. beamforming, phase_masks and pulse filtering 
        bb_signal    = [None for c in range(nChannels)] 
        for chIndex, currentSequence in enumerate(self.gpu.sequences[swing]):
            if currentSequence != None:
                bb_signal[chIndex]  = self.generate_bb_signal(currentSequence, shapefilter = dsp_filters.gaussian_pulse)
                
        # synthesize rf waveform (up mixing in cuda)

        self.logger.warning('TODO: refactor cuda_generate_pulse_handler to fix accessing rx bb samples per integration period by hardcoded first sequence')
        self.gpu.synth_channels(bb_signal, swing)

        
        # copy rf waveform to shared memory from GPU memory 
        acquire_sem( tx_sem_list[SIDEA][swing])
        self.gpu.txsamples_host_to_shm(swing)

        if os.path.isfile('./cuda.dump.tx'):
             name = datetime.now().strftime('%Y-%m-%d_%H%M%S')
             with open('/data/diagnostic_samples/cuda_dump_tx_'+ name + '.pkl', 'wb') as f:
                  pickle.dump(self.gpu.tx_rf_outdata, f, pickle.HIGHEST_PROTOCOL)
             os.remove('cuda.dump.tx') # only save tx samples once
             self.logger.info('Wrote raw tx samples to /data/diagnostic_samples/cuda_dump_tx_'+ name + '.pkl')

        self.logger.debug('finishing generate_pulse, releasing semaphores') 

        release_sem(tx_sem_list[SIDEA][swing])

        self.logger.debug('semaphores released') 

    # generate baseband sample vectors for a transmit pulse from sequence information
    def generate_bb_signal(self, channel, shapefilter = None):
        self.logger.debug('entering generate_bb_signal')
        # tpulse is the pulse time in seconds
        # bbrate is the transmit baseband sample rate 
        # tfreq is transmit freq in hz
        # tbuffer is guard time between tr gate and rf pulse in seconds

        ctrlprm = channel.ctrlprm

        tpulse = channel.pulse_lens[0] / 1e6 # read in array of pulse lengths in seconds 
        tx_center_freq = ctrlprm['tfreq'] * 1000 # read in tfreq, convert from kHz to Hz
    
        tbuffer = float(channel.tr_to_pulse_delay) / 1e6 # convert microseconds tbuffer in config file to seconds

        trise = ctrlprm['trise'] / 1e9 # read in rise time in seconds (TODO: fix units..)
        # TODO assert trise < tx_bb_samplingRate when I'm sure what is trise
        assert tbuffer >= int(self.hardware_limits['min_tr_to_pulse']) / 1e6, 'time between TR gate and RF pulse too short for hardware'
        assert tpulse > int(self.hardware_limits['min_chip']) / 1e6, 'pulse length ({} micro s) is too short for hardware (min is {} micro s)'.format(tpulse*1e6, self.hardware_limits['min_chip'])
        assert tpulse < int(self.hardware_limits['max_tpulse']) / 1e6, 'pulse length is too long for hardware'

        if not (tx_center_freq >= int(self.hardware_limits['minimum_tfreq'])):
            self.logger.error('transmit center frequency too low for hardware')

        if tx_center_freq > int(self.hardware_limits['maximum_tfreq']):
            self.logger.error('transmit center frequency too high for hardware')

        #assert tfreq < int(self.hardware_limits['maximum_tfreq']), 'transmit frequency too high for hardware'

        assert sum(channel.pulse_lens) / (1e6 * ((channel.pulse_offsets_vector[-1] + tpulse))) < float(self.hardware_limits['max_dutycycle']), ' duty cycle of pulse sequence is too high'
        
        nPulses   = len(channel.pulse_lens)
        nAntennas = len(self.antennas)
        
        # tbuffer is the time between tr gate and transmit pulse 
        padding    = np.zeros(int(np.round(tbuffer * self.gpu.tx_bb_samplingRate)))
        pulse      = np.ones( int(np.round(tpulse  * self.gpu.tx_bb_samplingRate)))
        pulsesamps = np.complex64(np.concatenate([padding, pulse, padding]))

        self.logger.debug('create BB pulses: tbuffer: {} us, tpulse: {} us, nSamples: {} '.format(tbuffer*1e6, tpulse*1e6, pulsesamps.shape[0]))

        beam_sep  = float(self.array_info['beam_sep']) # degrees
        nbeams    = int(self.array_info['nbeams'])
        x_spacing = float(self.array_info['x_spacing']) # meters
        beamnum   = ctrlprm['tbeam']

        # convert beam number of radian angle
        bmazm = calc_beam_azm_rad(nbeams, beamnum, beam_sep)

        # calculate antenna-to-antenna phase shift for steering at a frequency
        pshift = calc_phase_increment(bmazm, tx_center_freq, x_spacing)

        # calculate a complex number representing the phase shift for each antenna
        beamforming_shift = [rad_to_rect(a * pshift) for a in self.antennas]
        
        # construct baseband tx sample array
        bb_signal = np.complex128(np.zeros((nAntennas, nPulses, len(pulsesamps))))

        self.logger.debug("ChannelScalingFactor is {} for channel {} (tfreq={} MHz). ".format(channel.channelScalingFactor, channel.ctrlprm['channel'],channel.ctrlprm['tfreq']/1000 ))

        if channel.channelScalingFactor == 0:
            self.logger.warning("ChannelScalingFactor is zero for channel {} (tfreq={} MHz). Channel is muted! ".format(channel.ctrlprm['channel'],channel.ctrlprm['tfreq'] / 1000 ))

        for iAntenna in range(nAntennas):
            for iPulse in range(nPulses):
                # compute pulse compression 

                # apply phase shifting to pulse using phase_mask
                psamp = np.copy(pulsesamps)
                psamp[len(padding):-len(padding)] *=  np.exp(1j * np.pi * channel.phase_masks[iPulse])
                # TODO: support non-1us resolution phase masks
        
                # apply filtering function
                if shapefilter != None:
                    psamp = shapefilter(psamp, trise, self.gpu.tx_bb_samplingRate)
                
                # apply beamforming
                psamp *= beamforming_shift[iAntenna]

                # apply gain control
                psamp *= channel.channelScalingFactor

                # update baseband pulse sample array for antenna
                bb_signal[iAntenna][iPulse] = psamp

        return bb_signal
         
# add a new channel 
class cuda_add_channel_handler(cudamsg_handler):
    def process(self):
        self.logger.debug('entering cuda_add_channel_handler')

        # get information from usrp_server
        cmd = cuda_add_channel_command([self.sock])
        cmd.receive(self.sock) 
        swing    = cmd.payload['swing']
        sequence = cmd.sequence
        self.logger.debug('received sequence, swing {}'.format(swing))


        self.logger.debug('entering cuda_add_channel_handler, waiting for swing semaphores')
        acquire_sem(rx_sem_list[SIDEA][swing])

        # determine (internal cuda) channel index
        channelNumber = sequence.ctrlprm['channel']
        if channelNumber in self.gpu.channelNumbers[swing]:
            chIdx = self.gpu.channelNumbers[swing].index(channelNumber)
            self.logger.warning("Channel {} already added at idx {}. Reinitializing it ...".format(channelNumber, chIdx))
        else:
            if None in self.gpu.channelNumbers[swing]:
                chIdx = self.gpu.channelNumbers[swing].index(None)
                self.gpu.channelNumbers[swing][chIdx] = channelNumber
            else:
                self.logger.error("all channles active, can not add channel")
                return # TODO rerun error?

        self.gpu.sequences[swing][chIdx] = sequence


        if verbose:
           self.logger.debug("===> adding new channel, swing {}:".format(swing))
           self.logger.debug("  channel number {}  (cuda index: {})".format(channelNumber, chIdx))
           self.logger.debug("  tx channel freq {} kHz".format(self.gpu.sequences[swing][chIdx].ctrlprm['tfreq'] ))
           self.logger.debug("  rx channel freq {} kHz".format(self.gpu.sequences[swing][chIdx].ctrlprm['rfreq'] ))
           self.logger.debug("  tx pulse offset (us)  "+ str(  self.gpu.sequences[swing][chIdx].pulse_offsets_vector) )

        # TODO: think if this has to move to rx/tx handler...
#        this is the next step
#        self.gpu._set_tx_mixerfreq(chIdx, swing)
#        self.gpu._set_tx_phasedelay(chIdx, swing)
#        self.gpu._set_rx_phaseIncrement(chIdx, swing)
 
        # release semaphores
        release_sem(rx_sem_list[SIDEA][swing])
        self.logger.debug('semaphores released and leaving cuda_add_channel_handler')

# remove a channel from the GPU
class cuda_remove_channel_handler(cudamsg_handler):
    def process(self):
        cmd = cuda_remove_channel_command([self.sock])
        cmd.receive(self.sock)
        swing = cmp.payload['swing']

        sequence = cmd.sequence
        channelNumber = sequence.ctrlprm['channel']

        if channelNumber in self.gpu.channelNumbers[swing]:
            chIdx = self.gpu.channelNumbers[swing].index(channelNumber)
            self.gpu.sequences[swing][chIdx] = None
            self.gpu.channelNumbers[swing][chIdx] = None
            self.logger.debug("Delete Channel {}  at idx {}. ".format(channelNumber, chIdx))
        else:
            self.logger.warning("cuda remove channel: No channel {} in cuda. (channel numbers : {}) swing {}".format(channelNumber, self.gpu.channelNumbers[swing], swing))

        



# take copy and process data from shared memory, send to usrp_server via socks 
class cuda_get_data_handler(cudamsg_handler):
    def process(self):
        self.logger.debug('entering cuda_get_data handler')

        cmd = cuda_get_data_command([self.sock])
        cmd.receive(self.sock)
        swing    = cmd.payload['swing']    

        self.logger.debug('pulling data from gpu memory...(swing {})'.format(swing))
        samples = self.gpu.pull_rxdata(swing)
        nAntennas, nChannels, nSamples = samples.shape
        self.logger.debug('finished pulling baseband data from GPU, format: (antennas, channels, samples): {}'.format(samples.shape))

        self.logger.debug('transmitting nAntennas {}'.format(nAntennas)) 
        transmit_dtype(self.sock, nAntennas, np.uint32)
        # transmit requested channels
        channel = recv_dtype(self.sock, np.int32) 
        while (channel != -1):
            channelIndex = self.gpu.channelNumbers[swing].index(channel)
            self.logger.debug('received channel number ={}(cuda index: {}))'.format(channel, channelIndex))

            for iAntenna in range(nAntennas):
                transmit_dtype(self.sock, self.antennas[iAntenna], np.uint16)
                self.logger.debug('transmitted antenna index {}'.format(iAntenna))
                transmit_dtype(self.sock, nSamples, np.uint32)
                self.logger.debug('transmitted number of samples ({})'.format(nSamples))
                self.logger.debug(" transmitting sum : {}".format(np.sum(samples[iAntenna][channelIndex]) ))
                self.sock.sendall(samples[iAntenna][channelIndex].tobytes())

            channel = recv_dtype(self.sock, np.int32) 

#       release_sem(rx_sem_list[SIDEA][swing]) # TODO why is this here so lonely? (mgu)


# copy data to gpu, start processing
class cuda_process_handler(cudamsg_handler):
    def process(self):
        cmd = cuda_process_command([self.sock])
        cmd.receive(self.sock)
        swing = cmd.payload['swing']
        self.logger.debug('enter cuda_process_handler (swing {})'.format(swing))
#        pdb.set_trace()

#        acquire_sem(rx_sem_list[SIDEA][swing])
        self.gpu.rx_init(swing)

        self.gpu.rxsamples_shm_to_gpu(rx_shm_list[SIDEA][swing])
        self.gpu._set_rx_phaseIncrement(swing) 
        self.gpu.rxsamples_process(swing) 
 #       release_sem(rx_sem_list[SIDEA][swing])
        self.logger.debug('leaving cuda_process_handler (swing {})'.format(swing))


# cleanly exit.
class cuda_exit_handler(cudamsg_handler):
    def process(self):
        cmd = cuda_exit_command([self.sock])
        cmd.receive(self.sock)

        clean_exit() 


# NOT USED:
# allocate memory for cuda sample buffers
class cuda_setup_handler(cudamsg_handler):
    def process(self):
        cmd = cuda_setup_command([self.sock])
        cmd.receive(self.sock)
#        swing = cmd.payload['swing']
        self.logger.debug('entering cuda_setup_handler (currently blank!)')
        acquire_sem(rx_sem_list[SIDEA][SWING0])
        acquire_sem(rx_sem_list[SIDEA][SWING1])
        
        # release semaphores
        release_sem(rx_sem_list[SIDEA][SWING0])
        release_sem(rx_sem_list[SIDEA][SWING1])

# NOT USED:    
# prepare for a refresh of sequences 
class cuda_pulse_init_handler(cudamsg_handler):
    def process(self):
        self.logger.debug('entering cuda_pulse_init_handler')
        cmd = cuda_pulse_init_command([self.sock])
        cmd.receive(self.sock)
        swing # TODO: receive        
        acquire_sem(rx_sem_list[SIDEA][swing])
        release_sem(rx_sem_list[SIDEA][swing])

cudamsg_handlers = {\
        CUDA_SETUP: cuda_setup_handler, \
        CUDA_GET_DATA: cuda_get_data_handler, \
        CUDA_PROCESS: cuda_process_handler, \
        CUDA_ADD_CHANNEL: cuda_add_channel_handler, \
        CUDA_REMOVE_CHANNEL: cuda_remove_channel_handler, \
        CUDA_GENERATE_PULSE: cuda_generate_pulse_handler, \
        CUDA_PULSE_INIT : cuda_pulse_init_handler, \
        CUDA_EXIT: cuda_exit_handler}

cudamsg_handler_names = {\
        CUDA_SETUP: 'CUDA_SETUP', \
        CUDA_GET_DATA: 'CUDA_GET_DATA', \
        CUDA_PROCESS: 'CUDA_PROCESS', \
        CUDA_ADD_CHANNEL: 'CUDA_ADD_CHANNEL', \
        CUDA_REMOVE_CHANNEL: 'CUDA_REMOVE_CHANNEL', \
        CUDA_GENERATE_PULSE: 'CUDA_GENERATE_PULSE', \
        CUDA_PULSE_INIT : 'CUDA_PULSE_INIT', \
        CUDA_EXIT: 'CUDA_EXIT'}


def sem_namer(ant, swing, direction):
    name = 'semaphore_{}_ant_{}_swing_{}'.format(direction, int(ant), int(swing))
    return name

def shm_namer(antenna, swing, side, direction):
    name = 'shm_{}_ant_{}_side_{}_swing_{}'.format(direction, int(antenna), int(side), int(swing))
    return name

def create_shm(antenna, swing, side, shm_size, direction):
    name = shm_namer(antenna, swing, side, direction)
    memory = posix_ipc.SharedMemory(name, posix_ipc.O_CREAT, size=int(shm_size))
    mapfile = mmap.mmap(memory.fd, memory.size)
    memory.close_fd()
    shm_list.append(name)
    return mapfile

def create_sem(ant, swing, direction):
    name = sem_namer(ant, swing, direction)
    sem = posix_ipc.Semaphore(name, posix_ipc.O_CREAT)
    sem.release()
    sem_list.append(sem)
    return sem

def clean_exit():
    print("CUDA: clean_exit()")
    for shm in shm_list:
        posix_ipc.unlink_shared_memory(shm)

    for sem in sem_list:
        sem.release()
        sem.unlink()
        
    sys.exit(0)

def sigint_handler(signum, frame):
   clean_exit()

# class to contain references to gpu-side information
# handle launching signal processing kernels
# and host/gpu communication and initialization
# bb_signal is now [NANTS, NPULSES, NCHANNELS, NSAMPLES]
class ProcessingGPU(object):
    def __init__(self, antennas, maxchannels, maxpulses, ntapsrx_rfif, ntapsrx_ifbb, rfifrate, ifbbrate, fsamptx, fsamprx, txupsamplerate):

        self.logger = logging.getLogger("cuda_gpu")
        self.logger.info('initializing cuda gpu')
        self.antennas = np.int16(antennas)
        # maximum supported channels
        self.nChannels = int(maxchannels)
        self.nAntennas = len(antennas)
        self.nPulses   = int(maxpulses)

        # number of taps for baseband and if filters
        self.ntaps_rfif = int(ntapsrx_rfif)
        self.ntaps_ifbb = int(ntapsrx_ifbb)

        # rf to if downsampling ratio 
        self.rx_rf2if_downsamplingRate = int(rfifrate)
        self.rx_if2bb_downsamplingRate = int(ifbbrate)

        # USRP rx/tx sampling rates
        self.tx_rf_samplingRate = int(fsamptx)
        self.rx_rf_samplingRate = int(fsamprx)

        # USRP NCO mixing frequency TODO: get from usrp_server
        self.usrp_mixing_freq = [13e6, 13e6]
        
        self.tx_upsamplingRate = int(txupsamplerate) #  TODO: adjust name later
        # calc base band sampling rates 
        self.tx_bb_samplingRate = self.tx_rf_samplingRate / self.tx_upsamplingRate
        self.rx_bb_samplingRate = self.rx_rf_samplingRate / self.rx_rf2if_downsamplingRate / self.rx_if2bb_downsamplingRate

        # calibration tables for phase and time delay offsets
        self.tdelays = np.zeros(self.nAntennas) # table to account for constant time delay to antenna, e.g cable length difference
        self.phase_offsets = np.zeros(self.nAntennas) # table to account for constant phase offset, e.g 180 degree phase flip
 
        self.sequences      = [ [None for iCh in range(self.nChannels)] for iSwing in allSwings] # table to store sequence infomation
        self.channelNumbers = [ [None for iCh in range(self.nChannels)] for iSwing in allSwings] # cuda sequence index to channel number (cnum)

        # host side copy of channel transmit frequency array
        self.tx_mixer_freqs = np.zeros(self.nChannels, dtype=np.float64)# TODO: delete from class and use local variabe 
       
        # host side copy of rx part: rx frequency and decimation rates
        self.rx_phaseIncrement_rad = np.zeros(self.nChannels, dtype=np.float64)# TODO: delete from class and use local variabe
        self.rx_decimationRates    = np.zeros(2, dtype=np.int16)
       
        # host side copy of per-channel, per-antenna array with calibrated cable phase offset
        self.phase_delays = np.zeros((self.nChannels, self.nAntennas), dtype=np.float32)# TODO: delete from class and use local variabe
        # dictionaries to map usrp array indexes and sequence channels to indexes 
        self.channel_to_idx = {}
        
        with open('rx_cuda.cu', 'r') as f:
            self.cu_rx = pycuda.compiler.SourceModule(f.read())
            self.cu_rx_multiply_and_add   = self.cu_rx.get_function('multiply_and_add')
            self.cu_rx_multiply_mix_add   = self.cu_rx.get_function('multiply_mix_add')
            self.cu_rx_phaseIncrement_rad = self.cu_rx.get_global('phaseIncrement_NCO_rad')[0]
            self.cu_rx_decimationRates    = self.cu_rx.get_global('decimationRates')[0]

        
        with open('tx_cuda.cu', 'r') as f:
            self.cu_tx = pycuda.compiler.SourceModule(f.read())
            self.cu_tx_interpolate_and_multiply = self.cu_tx.get_function('interpolate_and_multiply')
            self.cu_tx_mixer_freq_rads = self.cu_tx.get_global('txfreq_rads')[0]
            self.cu_txoffsets_rads = self.cu_tx.get_global('txphasedelay_rads')[0]
                
        self.streams = [cuda.Stream() for i in range(self.nChannels)]

    # add a USRP with some constant calibration time delay and phase offset (should be frequency dependant?)
    # instead, calibrate VNA on one path then measure S2P of other paths, use S2P file as calibration?
    def addUSRP(self, usrp_hostname = '', driver_hostname = '', mainarray = True, array_idx = -1, x_position = None, tdelay = 0, side = 'a', phase_offset = None):
        self.tdelays[int(array_idx)] = tdelay
        self.phase_offsets[int(array_idx)] = phase_offset
    
    # generate tx rf samples from sequence
    def synth_tx_rf_pulses(self, bb_signal, tx_bb_nSamples_per_pulse, swing):
        for iChannel in range(self.nChannels):
            if self.sequences[swing][iChannel] != None:  # if channel is defined
                for (iAntenna, ant) in enumerate(self.antennas):
                    for iPulse in range(bb_signal[iChannel].shape[1]):
                        # bb_signal[channel][nantennas, nPulses, len(pulsesamps)]
                        # create interleaved real/complex bb vector
                        bb_vec_interleaved = np.zeros(tx_bb_nSamples_per_pulse * 2)
                        try:
                            self.logger.debug('synth_tx_rf_pulses: copying baseband signals from channel: {}, antenna: {}, pulse: {}'.format(iChannel, iAntenna, iPulse))
                            bb_vec_interleaved[0::2] = np.real(bb_signal[iChannel][iAntenna][iPulse][:])
                            bb_vec_interleaved[1::2] = np.imag(bb_signal[iChannel][iAntenna][iPulse][:])
                        except:
                            self.logger.error('error while merging baseband tx vectors..')
                            pdb.set_trace()

                        self.tx_bb_indata[iAntenna][iChannel][iPulse][:] = bb_vec_interleaved[:]

        self._set_tx_mixerfreq(swing)
        self._set_tx_phasedelay(swing)
        
        # upsample baseband samples on GPU, write samples to shared memory
        self.interpolate_and_multiply()
        cuda.Context.synchronize()
    
    def tx_init(self, tx_bb_nSamples_per_pulse):
        

        # calculate the number of rf samples per pulse 
        tx_rf_nSamples_per_pulse = int( tx_bb_nSamples_per_pulse * self.tx_upsamplingRate) # number of rf samples for all pulses
        tx_rf_nSamples_total = tx_rf_nSamples_per_pulse * self.nPulses

        self.logger.debug('tx_init: tx_rf_nSamples_total: {}, nAntennas: {} '.format(tx_rf_nSamples_total, self.nAntennas))
 
        # allocate page-locked memory on host for rf samples to decrease transfer time
        # TODO: benchmark this, see if I should move this to init function..
        self.tx_rf_outdata = cuda.pagelocked_empty((self.nAntennas, 2 * tx_rf_nSamples_total), np.int16, mem_flags=cuda.host_alloc_flags.DEVICEMAP)
        self.tx_bb_indata  = np.float32(np.zeros(  [self.nAntennas, self.nChannels, self.nPulses, tx_bb_nSamples_per_pulse * 2])) # * 2 pulse samples for interleaved i/q

        # TODO: look into memorypool and freeing page locked memory?
        # https://stackoverflow.com/questions/7651450/how-to-create-page-locked-memory-from-a-existing-numpy-array-in-pycuda

        # point GPU to page-locked memory for rf rx and tx samples
        self.cu_tx_rf_outdata = np.intp(self.tx_rf_outdata.base.get_device_pointer())
        self.cu_tx_bb_indata  = cuda.mem_alloc_like(self.tx_bb_indata)
       
        # compute grid/block sizes for cuda kernels
        self.tx_block = self._intify((self.tx_upsamplingRate, self.nChannels, 1))
        self.tx_grid  = self._intify((tx_bb_nSamples_per_pulse, self.nAntennas, self.nPulses))
        
        if self.logger.isEnabledFor(logging.DEBUG): # save time 
           self.logger.debug("= PARAMETER: ")
           self.logger.debug("     nChannels       : {} ".format( self.nChannels ))
           self.logger.debug("     nAntennas       : {} ".format( self.nAntennas ))
           self.logger.debug("     nPulse          : {} ".format( self.nPulses))
           self.logger.debug(" TX :")
           self.logger.debug("   upsampling rate : {}x".format( self.tx_upsamplingRate ))

           self.logger.debug(" BB  Sampling Rate    :  {} kHz".format(self.tx_bb_samplingRate / 1000 ))
           self.logger.debug(" BB  nSamples per Puse:  {}".format( tx_bb_nSamples_per_pulse))


           self.logger.debug(" RF  Sampling Rate    :  {} kHz".format(self.tx_rf_samplingRate / 1000 ))
           self.logger.debug(" RF  nSamples per Puse:  {}".format( tx_rf_nSamples_per_pulse))


           self.logger.debug('  TX Block: {}'.format( str(self.tx_block)))
           self.logger.debug('  TX Grid:  {}'.format( str(self.tx_grid )))

# some of the rx variable do not exist, since rx_init moved to process_handler
#           self.logger.debug("RX RF Sampling Rate    :  {} kHz".format(self.rx_rf_samplingRate / 1000 ))
#           self.logger.debug("RF RX nSamples         :  {}".format(self.rx_rf_nSamples))
#           self.logger.debug("RF => IF")
#           self.logger.debug(" downsampling rf => if : {}x ".format( self.rx_rf2if_downsamplingRate))
#           self.logger.debug('  RX Block rf => if : {}'.format( str(self.rx_if_block)))
#           self.logger.debug('  RX Grid  rf => if : {}'.format(str(self.rx_if_grid )))
#
#           self.logger.debug("RF => IF")
#           self.logger.debug(" downsampling if => bb : {}x ".format( self.rx_if2bb_downsamplingRate ))
#           self.logger.debug('  RX Block if => bb : {}'.format( str(self.rx_bb_block)))
#           self.logger.debug('  RX Grid  if => bb : {}'.format( str(self.rx_bb_grid )))
#
#           self.logger.debug(" BB Sampling Rate    :  {} kHz".format(self.rx_bb_samplingRate / 1000 ))

 
        max_threadsPerBlock = cuda.Device(0).get_attribute(pycuda._driver.device_attribute.MAX_THREADS_PER_BLOCK)
        assert self._threadsPerBlock(self.tx_block) <= max_threadsPerBlock, 'tx upsampling block size exceeds CUDA limits, reduce stage upsampling rate, number of pulses, or number of channels'


    def rx_init(self, swing): 
        # build arrays based on first sequence.
        seq = self.sequences[swing][0] # TODO: check if seq[0] exists
        ctrlprm = seq.ctrlprm
        
        decimationRate_rf2if = self.rx_rf2if_downsamplingRate
        decimationRate_if2bb = self.rx_if2bb_downsamplingRate

        # copy decimation rates to rx_cuda
        self.rx_decimationRates[:] = (int(decimationRate_rf2if), int(decimationRate_if2bb))
        cuda.memcpy_htod(self.cu_rx_decimationRates, self.rx_decimationRates)

        rx_bb_samplingRate = ctrlprm['baseband_samplerate']
        assert rx_bb_samplingRate != self.rx_bb_samplingRate, "rf_samplingRate and decimation rates of ini file does not result in rx_bb_samplingRate requested from control program"
        rx_bb_nSamples = seq.nbb_rx_samples_per_integration_period

#        # OLD: now we start with nSamples_rx_rf
#        rx_bb_nSamples = seq.nbb_rx_samples_per_integration_period
#        # calculate exact number of if and rf samples (based on downsampling and filtering (valid output))
#        rx_if_nSamples      = int((rx_bb_nSamples-1) * decimationRate_if2bb + self.ntaps_ifbb)
#        self.rx_rf_nSamples = int((rx_if_nSamples-1) * decimationRate_rf2if + self.ntaps_rfif)
        self.rx_rf_nSamples = int(seq.nbb_rx_samples_per_integration_period)
        rx_if_nSamples      = int( (self.rx_rf_nSamples - self.ntaps_rfif + 1 ) / decimationRate_rf2if + 1 )
        rx_bb_nSamples      = int( (rx_if_nSamples      - self.ntaps_ifbb + 1 ) / decimationRate_if2bb +1  )

        self.logger.debug("nSamples_rx: bb={}, if={}, rf={}".format(rx_bb_nSamples, rx_if_nSamples, self.rx_rf_nSamples))

        # calculate rx sample decimation rates
        rx_time = rx_bb_nSamples / rx_bb_samplingRate
        

        # [NCHANNELS][NTAPS][I/Q]
        self.rx_filtertap_rfif = np.float32(np.zeros([self.nChannels, self.ntaps_rfif, 2]))
        self.rx_filtertap_ifbb = np.float32(np.zeros([self.nChannels, self.ntaps_ifbb, 2]))
    
        # generate filters
        channelFreqVec = [None for i in range(self.nChannels)]
        for iChannel in range(self.nChannels):
            if self.sequences[swing][iChannel] != None:
               channelFreqVec[iChannel] = -( self.sequences[swing][iChannel].ctrlprm['rfreq']*1000 - self.usrp_mixing_freq[swing]) # use negative frequency here since filter is not time inverted for convolution
               self.logger.debug('generating rx filter for ch {}: {} kHz (USRP baseband: {} kHz)'.format(iChannel, self.sequences[swing][iChannel].ctrlprm['rfreq'],  self.sequences[swing][iChannel].ctrlprm['rfreq'] - self.usrp_mixing_freq[swing] /1000 ))
 

        self.rx_filtertap_rfif = dsp_filters.kaiser_filter_s0(self.ntaps_rfif, channelFreqVec, self.rx_rf_samplingRate)    
        # dsp_filters.rolloff_filter_s1()
        self.rx_filtertap_ifbb = dsp_filters.raisedCosine_filter(self.ntaps_ifbb, self.nChannels)
    
#        self._plot_filter()
        
        self.rx_if_samples = np.float32(np.zeros([self.nAntennas, self.nChannels, 2 * rx_if_nSamples]))
        self.rx_bb_samples = np.float32(np.zeros([self.nAntennas, self.nChannels, 2 * rx_bb_nSamples]))

        self.rx_rf_samples = cuda.pagelocked_empty((self.nAntennas,  self.rx_rf_nSamples*2), np.int16, mem_flags=cuda.host_alloc_flags.DEVICEMAP)

        self.cu_rx_samples_rf = np.intp(self.rx_rf_samples.base.get_device_pointer())

        # allocate memory on GPU
        self.cu_rx_filtertaps_rfif = cuda.mem_alloc_like(self.rx_filtertap_rfif)
        self.cu_rx_filtertaps_ifbb = cuda.mem_alloc_like(self.rx_filtertap_ifbb)

        self.cu_rx_if_samples = cuda.mem_alloc_like(self.rx_if_samples)
        self.cu_rx_bb_samples = cuda.mem_alloc_like(self.rx_bb_samples)
       
        cuda.memcpy_htod(self.cu_rx_filtertaps_rfif, self.rx_filtertap_rfif)
        cuda.memcpy_htod(self.cu_rx_filtertaps_ifbb, self.rx_filtertap_ifbb)
 
        # define cuda grid and block sizes
        self.rx_if_grid  = self._intify((rx_if_nSamples, self.nAntennas, 1))
        self.rx_bb_grid  = self._intify((rx_bb_nSamples, self.nAntennas, 1))
        self.rx_if_block = self._intify((self.ntaps_rfif / 2, self.nChannels, 1))
        self.rx_bb_block = self._intify((self.ntaps_ifbb / 2, self.nChannels, 1))
        
        # check if up/downsampling cuda kernels block sizes exceed hardware limits 
        max_threadsPerBlock = cuda.Device(0).get_attribute(pycuda._driver.device_attribute.MAX_THREADS_PER_BLOCK)

        assert self._threadsPerBlock(self.rx_if_block) <= max_threadsPerBlock, 'rf to if block size exceeds CUDA limits, reduce downsampling rate, number of pulses, or number of channels'
        assert self._threadsPerBlock(self.rx_bb_block) <= max_threadsPerBlock, 'if to bb block size exceeds CUDA limits, reduce downsampling rate, number of pulses, or number of channels'


        # synthesize rf waveform (beamforming, apply phase_masks, mixing in cuda)
    def synth_channels(self, bb_signal, swing):
#        self.rx_init(swing) moved to process_handler

        # TODO: this assumes all channels have the same number of samples 
        tx_bb_nSamples_per_pulse = int(bb_signal[0].shape[2]) # number of baseband samples per pulse
        self.tx_init(tx_bb_nSamples_per_pulse)

        self.synth_tx_rf_pulses(bb_signal, tx_bb_nSamples_per_pulse, swing)
    
        # Debug plotting for TX
        debugAmpCompare = False # compare the ampitude of input and output of usrp (out connected to in)
        if debugAmpCompare:
            import matplotlib.pyplot as plt
            plt.plot(self.tx_rf_outdata[0][0::2])
            maxOutValue = np.max(np.absolute(self.tx_rf_outdata[0][0::2]))
            maxOutValue = np.max(np.absolute(self.tx_rf_outdata[0][1::2]))
            plt.title("tx max value {}".format(maxOutValue))
            plt.show()
            

        if False:
        #  transmit pulse for debugging...
            import matplotlib
            #matplotlib.use('Agg')
            import matplotlib.pyplot as plt
            txpulse = self.tx_rf_outdata[0]
            arp  = np.sqrt(np.float32(txpulse[0::2]) ** 2 + np.float32(txpulse[1::2]) ** 2)

        #    plt.subplot(3,1,1)
        #    plt.plot(txpulse)
        #    plt.subplot(3,1,2)
        #    plt.plot(arp)
         #   plt.subplot(3,1,3)
         #   plt.plot(txpulse[0:5000:2])
         #   plt.plot(txpulse[1:5000:2])
           # plt.show()
        #    self.logger.debug('finished pulse generation, breakpoint..')
            import myPlotTools as mpt
            plt.figure()
            mpt.plot_freq(bb_signal[0][0][0], self.tx_bb_samplingRate, show=False)
            plt.title('one TX bb pulse')
            plt.gca().set_ylim([-200, 100])   
          
            plt.figure()
            mpt.plot_freq(txpulse[0:tx_bb_nSamples_per_pulse*self.tx_upsamplingRate*2], self.tx_rf_samplingRate, iqInterleaved=True, show=False)
            plt.gca().set_ylim([0, 150])
            plt.title('spectrum of one TX RF pulse')
            
            plt.show()
        
    # calculates the threads in a block from a block size tuple
    def _threadsPerBlock(self, block):
        return functools.reduce(np.multiply, block)

    # convert a tuple to ints for pycuda, blocks/grids must be passed as int tuples
    def _intify(self, tup):
        return tuple([int(v) for v in tup])

    # transfer rf samples from shm to memory pagelocked to gpu (TODO: make sure it is float32..)
    def rxsamples_shm_to_gpu(self, shm):      
        for aidx in range(self.nAntennas):
            shm[aidx].seek(0)
            self.rx_rf_samples[aidx] = np.frombuffer(shm[aidx], dtype=np.int16, count = self.rx_rf_nSamples*2)
        print(self.rx_rf_samples[0][:11]) 


               
    # kick off async data processing
    def rxsamples_process(self, swing):

        self.logger.debug('processing rf -> if')
        self.cu_rx_multiply_mix_add(self.cu_rx_samples_rf, self.cu_rx_if_samples, self.cu_rx_filtertaps_rfif, block = self.rx_if_block, grid = self.rx_if_grid, stream = self.streams[swing])
 
        self.logger.debug('processing if -> bb')
        self.cu_rx_multiply_and_add(self.cu_rx_if_samples, self.cu_rx_bb_samples, self.cu_rx_filtertaps_ifbb, block = self.rx_bb_block, grid = self.rx_bb_grid, stream = self.streams[swing])

        debugAmpCompare = False
        if debugAmpCompare:
            import matplotlib.pyplot as plt
            plt.plot(self.rx_rf_samples[0][0::2])
            maxInValue = np.max(np.absolute(self.rx_rf_samples[0][0::2]))
            maxInValue = np.max(np.absolute(self.rx_rf_samples[0][1::2]))
            plt.title("rx max value {}".format(maxInValue))
            plt.show()
        

        # for testing RX: plot RF, IF and BB
        if False:
            import myPlotTools as mpt
            import matplotlib.pyplot as plt
            #samplingRate_rx_bb =  self.gpu.sequence[0].ctrlprm['baseband_samplerate']
       #     plt.figure()        
            cuda.memcpy_dtoh(self.rx_if_samples, self.cu_rx_if_samples) 
            cuda.memcpy_dtoh(self.rx_bb_samples, self.cu_rx_bb_samples) 

            # PLOT all three frequency bands
            ax = plt.subplot(311)
            mpt.plot_freq(self.rx_rf_samples[0],  self.rx_rf_samplingRate, iqInterleaved=True, show=False)
            ax.set_ylim([50, 200])
            plt.ylabel('RF')

            ax = plt.subplot(312)
            mpt.plot_freq(self.rx_if_samples[0][0], self.rx_rf_samplingRate / self.rx_rf2if_downsamplingRate, iqInterleaved=True, show=False)
            mpt.plot_freq(self.rx_if_samples[0][1], self.rx_rf_samplingRate / self.rx_rf2if_downsamplingRate, iqInterleaved=True, show=False)
            ax.set_ylim([50, 200])
            plt.ylabel('IF')

            ax =plt.subplot(313)
            mpt.plot_freq(self.rx_bb_samples[0][0], self.rx_bb_samplingRate , iqInterleaved=True, show=False)
            ax.set_ylim([50, 200])
            plt.ylabel('BB')

            plt.figure()
            ax = plt.subplot(211) 
            mpt.plot_time(self.rx_rf_samples[0], self.rx_rf_samplingRate , iqInterleaved=True, show=False)
            plt.title("RF")

            ax = plt.subplot(212) 
            mpt.plot_time(self.rx_bb_samples[0][0], self.rx_bb_samplingRate , iqInterleaved=True, show=False)
            mpt.plot_time(self.rx_bb_samples[0][1], self.rx_bb_samplingRate , iqInterleaved=True, show=False)
            plt.title("BB")

            plt.show()

        #    mpt.plot_time_freq(self.rx_rf_samples[0][0][400000:440000], self.rx_rf_samplingRate, iqInterleaved=True)
        #    plt.title("Second pulse separate")

    # pull baseband samples from GPU into host memory
    def pull_rxdata(self, swing):
        self.streams[swing].synchronize()
        cuda.memcpy_dtoh(self.rx_bb_samples, self.cu_rx_bb_samples)
        if False:
           import matplotlib.pyplot as plt
           plt.figure()

           for iChannel in range(2):
              for iAnt in range(self.nAntennas):
                 plt.subplot(2 , self.nAntennas, iChannel*self.nAntennas + iAnt+1)
                 plt.plot(self.rx_bb_samples[iAnt][iChannel])
           plt.show() 
        return self.rx_bb_samples  #TODO remove the bb samples form class, if possible
    
    # upsample baseband data on gpu
    def interpolate_and_multiply(self):
        cuda.memcpy_htod(self.cu_tx_bb_indata, self.tx_bb_indata)
        self.cu_tx_interpolate_and_multiply(self.cu_tx_bb_indata, self.cu_tx_rf_outdata, block = self.tx_block, grid = self.tx_grid)
    
    # copy rf samples to shared memory for transmission by usrp driver
    def txsamples_host_to_shm(self, swing):
        # TODO: assumes single polarization
        for aidx in range(self.nAntennas):
            tx_shm_list[swing][aidx].seek(0)
            self.logger.debug('copy tx sampes to shm (ant {}): {}'.format( aidx, tx_shm_list[swing][aidx]))
            tx_shm_list[swing][aidx].write(self.tx_rf_outdata[aidx].tobytes())
            tx_shm_list[swing][aidx].flush()

    # update host-side mixer frequency table with current channel sequence, then refresh array on GPU
    def _set_tx_mixerfreq(self, swing):
        for channel in range(self.nChannels):
            if self.sequences[swing][channel] is not None:
               fc = self.sequences[swing][channel].ctrlprm['tfreq'] * 1000
               self.tx_mixer_freqs[channel] = np.float64(2 * np.pi * ( fc - self.usrp_mixing_freq[swing] ) / self.tx_rf_samplingRate)
               self.logger.debug('setting tx mixer freq for ch {}: {} MHz (usrp BB {} MHz) swing {}'.format(channel, fc/1e6, (fc - self.usrp_mixing_freq[swing])/1e6, swing)  )
               cuda.memcpy_htod(self.cu_tx_mixer_freq_rads, self.tx_mixer_freqs)
    
    # update pahse increment of NCO with current channel sequence, then refresh array on GPU
    def _set_rx_phaseIncrement(self,  swing):
        for channel in range(self.nChannels):
            if self.sequences[swing][channel] is not None:
               fc = self.sequences[swing][channel].ctrlprm['rfreq'] * 1000 
               self.rx_phaseIncrement_rad[channel] = np.float64(2 * np.pi * (  self.usrp_mixing_freq[swing] - fc ) / self.rx_rf_samplingRate)
               self.logger.debug('setting rx mixer freq (phase offset) for ch {}: {} MHz (usrp BB {} MHz) swing {} '.format(channel, fc/1e6, ( self.usrp_mixing_freq[swing] - fc)/1e6, swing)  )
               cuda.memcpy_htod(self.cu_rx_phaseIncrement_rad, self.rx_phaseIncrement_rad)

    # update host-side phase delay table with current channel sequence, then refresh array on GPU
    def _set_tx_phasedelay(self,  swing):
        for channel in range(self.nChannels):
            if self.sequences[swing][channel] is not None:
               fc = self.sequences[swing][channel].ctrlprm['tfreq'] * 1000      
               for iAntenna in range(self.nAntennas):
                   self.phase_delays[channel][iAntenna] = np.float32(np.mod(2 * np.pi  * self.tdelays[iAntenna] * fc +self.phase_offsets[iAntenna]/180*np.pi , 2 * np.pi)) 
       
               cuda.memcpy_htod(self.cu_txoffsets_rads, self.phase_delays)
   
    # plot filters 
    def _plot_filter(self):
        import matplotlib.pyplot as plt
        import myPlotTools as mpt
        iChannel = 0
        ax = plt.subplot(221)
        mpt.plot_time(self.rx_filtertap_rfif[iChannel,:,0] + 1j* self.rx_filtertap_rfif[iChannel,:,1], self.rx_rf_samplingRate, show=False)
        plt.title('RF Filter')

        ax = plt.subplot(222)
        mpt.plot_freq(self.rx_filtertap_rfif[iChannel,:,0] + 1j* self.rx_filtertap_rfif[iChannel,:,1], self.rx_rf_samplingRate, show=False)
        plt.title('RF Filter')
        ax.set_ylim([-60, 50])

        ax = plt.subplot(223)
        mpt.plot_time(self.rx_filtertap_ifbb[iChannel,:,0] + 1j* self.rx_filtertap_ifbb[iChannel,:,1], self.rx_rf_samplingRate / self.rx_rf2if_downsamplingRate, show=False)
        plt.title('IF Filter')

        ax = plt.subplot(224)
        mpt.plot_freq(self.rx_filtertap_ifbb[iChannel,:,0] + 1j* self.rx_filtertap_ifbb[iChannel,:,1], self.rx_rf_samplingRate / self.rx_rf2if_downsamplingRate, show=False)
        plt.title('IF Filter')
        ax.set_ylim([-60, 50])

        plt.show() 


# returns a list of antennas indexes in the usrp_config.ini file
def parse_usrpconfig_antennas(usrpconfig):
    main_antenna_list = []
    back_antenna_list = []
    for usrp in usrpconfig.sections():
        if usrpconfig[usrp]['mainarray'].lower() in ['true', '1']:
            main_antenna_list.append(int(usrpconfig[usrp]['array_idx']))
        else:
            back_antenna_list.append(int(usrpconfig[usrp]['array_idx']))
    return main_antenna_list, back_antenna_list

def acquire_sem(semList):
   for sem in semList:
      sem.acquire()

def release_sem(semList):
   for sem in semList:
      sem.release()



def main():
    logging_usrp.initLogging('cuda.log')
    logger = logging.getLogger('cuda_driver')

    # parse usrp config file, read in antennas list
    usrpconfig = configparser.ConfigParser()
    usrpconfig.read('../usrp_config.ini')

    main_antennas, back_antennas = parse_usrpconfig_antennas(usrpconfig)
    antennas = main_antennas + back_antennas

    # parse gpu config file
    cudadriverconfig = configparser.ConfigParser()
    cudadriverconfig.read('../driver_config.ini')
    shm_settings = cudadriverconfig['shm_settings']
    cuda_settings = cudadriverconfig['cuda_settings']
    network_settings = cudadriverconfig['network_settings']
   
    # parse array config file
    arrayconfig = configparser.ConfigParser()
    arrayconfig.read('../array_config.ini')
    array_info = arrayconfig['array_info']
    hardware_limits = arrayconfig['hardware_limits']

    # initalize cuda stuff
    gpu = ProcessingGPU(antennas, **cuda_settings)
    for usrp in usrpconfig.sections():
        gpu.addUSRP(**dict(usrpconfig[usrp]))
    
    # create command socket server to communicate with usrp_server.py
    cmd_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    cmd_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    cmd_sock.bind((network_settings.get('ServerHost'), network_settings.getint('CUDADriverPort')))
   
    # get size of shared memory buffer per-antenna in bytes from cudadriver_config.ini
    rxshm_size = shm_settings.getint('rxshm_size') 
    txshm_size = shm_settings.getint('txshm_size')
    
    # create shared memory buffers and semaphores for rx and tx
    for ant in antennas:
        for iSwing in allSwings:
            rx_shm_list[SIDEA][iSwing].append( create_shm(ant, iSwing, SIDEA, rxshm_size,  RXDIR))
            tx_shm_list[iSwing].append(        create_shm(ant, iSwing, SIDEA, txshm_size,  TXDIR))
            rx_sem_list[SIDEA][iSwing].append( create_sem(ant, iSwing, RXDIR))
            tx_sem_list[SIDEA][iSwing].append( create_sem(ant, iSwing, TXDIR)) 



    # create sigin_handler for graceful-ish cleanup on exit
    #signal.signal(signal.SIGINT, sigint_handler)

    logger.debug('cuda_driver waiting for socket connection at {}:{}'.format(network_settings.get('ServerHost'), network_settings.getint('CUDADriverPort')))

    # TODO: make this more.. robust, add error recovery..
    cmd_sock.listen(1)
    server_conn, addr = cmd_sock.accept()
    logger.debug('cuda_driver waiting for command')
   
    # wait for commands from usrp_server,  
    while(True):
        cmd = recv_dtype(server_conn, np.uint8)
        cmdname = cudamsg_handler_names[cmd]
        logger.debug('received {} command'.format(cmdname))

        handler = cudamsg_handlers[cmd](server_conn, cmd, gpu, antennas, array_info, hardware_limits)
        handler.process()

        logger.debug('finished processing {} command, responding'.format(cmdname))
        handler.respond()

        logger.debug('responded to {},  waiting for next command'.format(cmdname))
    

if __name__ == '__main__':
    main()