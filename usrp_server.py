#!/usr/bin/python3
# todo, write mock arbyserver that can handle these commands
# TODO: write mock arby server that can feed multiple normalscans with false data..

import sys
import numpy as np
import threading
import logging
import pdb
import socket
import time
import configparser

from termcolor import cprint
from phasing_utils import *
from socket_utils import *
from rosmsg import *
from drivermsg_library import *
from radar_config_constants import *
from clear_frequency_search import read_restrict_file, clrfreq_search

MAX_CHANNELS = 10
RMSG_FAILURE = -1
RMSG_SUCCESS = 0
RADAR_STATE_TIME = .0001
CHANNEL_STATE_TIMEOUT = 120000
# TODO: move this out to a config file
RESTRICT_FILE = '/home/radar/repos/SuperDARN_MSI_ROS/linux/home/radar/ros.3.6/tables/superdarn/site/site.kod/restrict.dat.inst'

debug = True

USRP_TRIGGER_START_DELAY = .5 # TODO: move this to a config file
# TODO: pull these from config file
STATE_INIT = 'INIT'
STATE_RESET = 'RESET'
STATE_WAIT = 'WAIT'
STATE_PRETRIGGER = 'PRETRIGGER'
STATE_TRIGGER = 'TRIGGER'
STATE_CLR_FREQ = 'CLR_FREQ_WAIT'
STATE_GET_DATA = 'GET_DATA'


# handle arbitration with multiple channels accessing the usrp hardware
# track state of a grouping of usrps
# merge information from multiple control programs, handle disparate settings
# e.g, ready flags and whatnot
class RadarHardwareManager:
    def __init__(self, port):
        self.client_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.client_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.client_sock.bind(('localhost', port))
        cprint('listening on port ' + str(port) + ' for control programs', 'blue')
        
        self.ini_file_init()
        self.usrp_init()
        #self.rxfe_init()
        self.cuda_init()

        self.restricted_frequencies = read_restrict_file(RESTRICT_FILE)
        self.nRegisteredChannels = 0

    def run(self):
        def spawn_channel(conn):
            # start new radar channel handler
            channel = RadarChannelHandler(conn, self)
            try:
                channel.run()
            except socket.error:
                print("RadarChannelHandler: Socket error => Deleting channel... ")
                self.deleteRadarChannel(channel)
   #         else:
   #             print("RadarChannelHandler: Other error => Deleting channel... ")
   #             self.deleteRadarChannel(channel)
   #             conn.close()
   #             print("RadarChannelHandler: Unexpected error is:", sys.exc_info()[0])
   #             raise

        # TODO: add lock support
        def radar_state_machine():
            cprint('starting radar hardware state machine', 'blue')
            self.state = STATE_INIT
            self.next_state = STATE_INIT
            self.addingNewChannelsAllowed = True    # no new channel should be added between PRETRIGGER and GET_DATA
            statePriorityOrder = [ STATE_CLR_FREQ, STATE_PRETRIGGER, STATE_TRIGGER, STATE_GET_DATA]
          
            sleepTime = 0.01 # used if state machine waits for one channel

            while True:
                self.state = self.next_state
                self.next_state = STATE_RESET

                if self.state == STATE_INIT:
                    # TODO: write init code?
                    self.next_state = STATE_WAIT

                if self.state == STATE_WAIT:
                    self.next_state = STATE_WAIT


                    # 1st priority is stateOrder, 2nd is channel number
                    for iState in statePriorityOrder:
                        if self.next_state == STATE_WAIT:      # if next state still not found
                            for iChannel in self.channels:
                                if iChannel.state == iState:
                                    self.next_state = iState
                                    break
                        else:
                            break
                        

                if self.state == STATE_CLR_FREQ:
                    # do a clear frequency search for channel requesting one
                    for ch in self.channels:
                        if ch.state == STATE_CLR_FREQ:
                            self.clrfreq(ch)
                            ch.state = STATE_WAIT
                    self.next_state = STATE_WAIT

                if self.state == STATE_PRETRIGGER:
                    self.next_state = STATE_WAIT
                    # loop through channels, see if anyone is not ready for pretrigger
                    # stay in pretrigger state until all channels are ready
                    # TODO: add timeout?
                    # (tuning the USRP, preparing combined rf samples on GPU)
                    for ch in self.channels:
                        if not ch.state in [ STATE_PRETRIGGER, STATE_TRIGGER]:
                            if ch.state  == STATE_CLR_FREQ:
                                print('STATE_MACHINE: setting state back to {} from {}'.format(ch.state, self.state))
                                self.next_state = ch.state
                            else:
                                print('STATE_MACHINE: remaining in PRETRIGGER because channel {} state is {}, '.format(ch.cnum, ch.state))
                                time.sleep(sleepTime)
                                self.next_state = STATE_PRETRIGGER

                    # if everyone is ready, prepare for data collection
                    if self.next_state == STATE_WAIT:
                        cprint('running pretrigger!', 'green')
                        self.addingNewChannelsAllowed = False
                        self.pretrigger()


                if self.state == STATE_TRIGGER:
                    self.next_state = STATE_WAIT
                    # wait for all channels to be in TRIGGER state
                    for ch in self.channels:
                        if not ch.state in [ STATE_TRIGGER, STATE_GET_DATA]:
                            if ch.state in [STATE_CLR_FREQ, STATE_PRETRIGGER]:
                                print('STATE_MACHINE: setting state back to {} from {}'.format(ch.state, self.state))
                                self.next_state = ch.state
                            else:
                                print('STATE_MACHINE: remaining in TRIGGER because channel {} state is {}, '.format(ch.cnum, ch.state))
                                time.sleep(sleepTime)
                                self.next_state = STATE_TRIGGER

                    # if all channels are TRIGGER, then TRIGGER and return to STATE_WAIT
                    if self.next_state == STATE_WAIT:
                        self.trigger()

                if self.state == STATE_GET_DATA:
                    self.next_state = STATE_WAIT

                    self.get_data(self.channels)

                    for ch in self.channels:
                        if ch.state == STATE_GET_DATA: 
                            ch.state = STATE_WAIT
                    self.addingNewChannelsAllowed = True


                if self.state == STATE_RESET:
                    cprint('stuck in STATE_RESET?!', 'yellow')
                    pdb.set_trace()
                    self.next_state = STATE_INIT

                time.sleep(RADAR_STATE_TIME)

        self.client_sock.listen(MAX_CHANNELS)
        client_threads = []
        self.channels = []

        ct = threading.Thread(target=radar_state_machine)
        ct.start()
        while True:

            cprint('waiting for control program', 'green')
            client_conn, addr = self.client_sock.accept()

            cprint('connection from control program, spawning channel handler thread', 'green')
            ct = threading.Thread(target=spawn_channel, args=(client_conn,))
            client_threads.append(ct)
            ct.start()
       
            # remove threads that are not alive
            client_threads = [iThread for iThread in client_threads if iThread.is_alive()]

 

        self.client_sock.close()


    # read in ini config files..
    def ini_file_init(self):
        # READ driver_config.ini
        driver_config = configparser.ConfigParser()
        driver_config.read('driver_config.ini')
        self.ini_shm_settings = driver_config['shm_settings']
        self.ini_cuda_settings = driver_config['cuda_settings']
        self.ini_network_settings = driver_config['network_settings']

        # READ usrp_config.ini
        usrp_config = configparser.ConfigParser()
        usrp_config.read('usrp_config.ini')
        usrp_configs = []
        for usrp in usrp_config.sections():
            usrp_configs.append(usrp_config[usrp])
        self.ini_usrp_configs = usrp_configs


        # READ array_config.ini
        array_config = configparser.ConfigParser()
        array_config.read('array_config.ini')
        self.ini_array_settings  = array_config['array_info']
      #  self.ini_hardware_limits = array_config['hardware_limits']



    def usrp_init(self):
        usrp_drivers = [] # hostname of usrp drivers
        usrp_driver_base_port = int(self.ini_network_settings['USRPDriverPort'])

        for usrp in self.ini_usrp_configs:
            usrp_driver_hostname = usrp['driver_hostname'] 
            usrp_driver_port = int(usrp['array_idx']) + usrp_driver_base_port 
            usrp_drivers.append((usrp_driver_hostname, usrp_driver_port))
	
        usrp_driver_socks = []
        
        # TODO: read these from ini config file
        # currently pulled from radar_config_constants.py
        self.usrp_tx_cfreq = DEFAULT_USRP_CENTER_FREQ
        self.usrp_rx_cfreq = DEFAULT_USRP_CENTER_FREQ 
        self.usrp_rf_tx_rate = int(self.ini_cuda_settings['FSampTX'])
        self.usrp_rf_rx_rate = int(self.ini_cuda_settings['FSampRX'])

        # open each 
        try:
            for usrp in usrp_drivers:
                cprint('connecting to usrp driver on port {}'.format(usrp[1]), 'blue')
                usrpsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                usrpsock.connect(usrp)
                usrp_driver_socks.append(usrpsock)
        except ConnectionRefusedError:
            cprint('USRP server connection failed', 'blue')
            sys.exit(1)

        self.usrpsocks = usrp_driver_socks
        self._resync_usrps()


    def _resync_usrps(self):
        usrps_synced = False

        while not usrps_synced:
            cmd = usrp_sync_time_command(self.usrpsocks)
            cmd.transmit()
            cmd.client_return()

            # once USRPs are connected, synchronize clocks/timers 
            cmd = usrp_get_time_command(self.usrpsocks)
            cmd.transmit() 

            usrptimes = []
            for usrpsock in self.usrpsocks:
                usrptimes.append(cmd.recv_time(usrpsock))
           
            cmd.client_return()
     
            # check if sync succeeded..
            if max(np.array(usrptimes) - usrptimes[0]) < 1:
                usrps_synced = True
            else:
                # TODO: why does USRP synchronization fail?
                print('USRP syncronization failed, trying again..')
                time.sleep(1)


    def rxfe_init(self):
        # TODO: fix this function
        pdb.set_trace()
        '''
        # TODO: read in rf_setting struct?
        amp0 = rf_settings[1] # amp1 in RXFESettings struct
        amp1 = rf_settings[2] # amp2 in RXFESettings struct
        att_p5dB = np.uint8((rf_settings[4] > 0))
        att_1dB = np.uint8((rf_settings[5] > 0))
        att_2dB = np.uint8((rf_settings[6] > 0))
        att_4dB = np.uint8((rf_settings[7] > 0))
        att = (att_p5dB) | (att_1dB << 1) | (att_2dB << 2) | (att_4dB << 3)
        '''
        cmd = usrp_rxfe_setup_command(self.usrpsocks, 0, 0, 0) #amp0, amp1, att)
        cmd.transmit()
        cmd.client_return()


    def cuda_init(self):
        #time.sleep(.05)

        self.tx_upsample_rate = int(self.ini_cuda_settings['TXUpsampleRate'])

        # connect cuda_driver servers
        cuda_driver_socks = []

        cuda_driver_port = int(self.ini_network_settings['CUDADriverPort'])
        cuda_driver_hostnames = self.ini_network_settings['CUDADriverHostnames'].split(',')

        try:
            for c in cuda_driver_hostnames:
                cudasock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                cudasock.connect((c, cuda_driver_port))
                cuda_driver_socks.append(cudasock)
        except ConnectionRefusedError:
            logging.warnings.warn("cuda server connection failed")
            sys.exit(1)

        self.cudasocks = cuda_driver_socks


    def setup(self, chan):
        pass

    def clrfreq(self, chan):
        cprint('running clrfreq for channel {}'.format(chan.cnum), 'blue')
        tbeamnum = chan.ctrlprm_struct.get_data('tbeam')
        tbeamwidth = 3.24 # TODO: why is it zero, this should be chan.ctrlprm_struct.get_data('tbeamwidth')
        chan.tfreq, chan.noise = clrfreq_search(chan.clrfreq_struct, self.usrpsocks, self.restricted_frequencies, tbeamnum, tbeamwidth, self.ini_array_settings['nbeams']) 
        chan.tfreq /= 1000 # clear frequency search stored in kHz for compatibility with control programs..

    def get_data(self, allChannels):
        nMainAntennas = int(self.ini_array_settings['main_ants'])
        nBackAntennas = int(self.ini_array_settings['back_ants'])
        cprint('running get_data', 'blue')

        nChannels = len(allChannels)

        # stall until all USRPs are ready
        cprint('sending USRP_READY_DATA command ', 'blue')
        cmd = usrp_ready_data_command(self.usrpsocks)
        cmd.transmit()
        cprint('sending USRP_READY_DATA command sent, waiting on READY_DATA status', 'blue')

        # alloc memory
        nSamples_bb  = self.commonChannelParameter['number_of_samples'] 
        main_samples = [np.complex64(np.zeros((nMainAntennas, nSamples_bb))) for iCh in range(nChannels) ]
        back_samples = [np.complex64(np.zeros((nBackAntennas, nSamples_bb))) for iCh in range(nChannels) ] 
        
        # check status of usrp drivers
        for usrpsock in self.usrpsocks:
            transmit_dtype(usrpsock, 11 , np.int32) # WHY DO THIS? (not used in usrp_driver) 

            ready_return = cmd.recv_metadata(usrpsock)
            rx_status = ready_return['status']

            cprint('GET_DATA rx status {}'.format(rx_status), 'blue')
            if rx_status != 2:
                cprint('USRP driver status {} in GET_DATA'.format(rx_status), 'blue')
                #status = USRP_DRIVER_ERROR # TODO: understand what is an error here..

        cmd.client_return()
        cprint('GET_DATA: received samples from USRP_DRIVERS, status: ' + str(rx_status), 'blue')

        # DOWNSAMPLING
        cprint('GET_DATA: send CUDA_PROCESS', 'blue')
        cmd = cuda_process_command(self.cudasocks)
        cmd.transmit()
        cmd.client_return()
        cprint('GET_DATA: finished CUDA_PROCESS', 'blue')

        # RECEIVER RX BB SAMPLES 
        # recv metadata from cuda drivers about antenna numbers and whatnot
        # fill up provided main and back baseband sample buffers
        cmd = cuda_get_data_command(self.cudasocks)
        cmd.transmit()

        for cudasock in self.cudasocks:
            nAntennas = recv_dtype(cudasock, np.uint32)
            for iChannel,channel in enumerate(allChannels):
                transmit_dtype(cudasock, channel.cnum, np.int32)

                for iAntenna in range(nAntennas):
                    antIdx = recv_dtype(cudasock, np.uint16)
                    num_samples = recv_dtype(cudasock, np.uint32)
                    samples = recv_dtype(cudasock, np.float32, num_samples)
                    samples = samples[0::2] + 1j * samples[1::2] # unpacked interleaved i/q
                    
                    if antIdx < nMainAntennas:
                        main_samples[iChannel][antIdx] = samples[:]

                    else:
                        back_samples[iChannel][antIdx - nMainAntennas] = samples[:]
                                
      
            transmit_dtype(cudasock, -1, np.int32) # to end transfer process
            
        cmd.client_return()
        cprint('get_data() tranfering data complete', 'blue')
        
        # BEAMFORMING
        def _beamform_uhd_samples(samples, phasing_matrix, n_samples, antennas):
            beamformed_samples = np.ones(n_samples)

            for i in range(n_samples):
                itemp = np.int16(0)
                qtemp = np.int16(0)

                for aidx in range(len(antennas)):
                    itemp += np.real(samples[aidx][i]) * np.real(phasing_matrix[aidx]) - \
                             np.imag(samples[aidx][i]) * np.imag(phasing_matrix[aidx])
                    qtemp += np.real(samples[aidx][i]) * np.imag(phasing_matrix[aidx]) + \
                             np.imag(samples[aidx][i]) * np.real(phasing_matrix[aidx])
                beamformed_samples[i] = complex_ui32_pack(itemp, qtemp)

            return beamformed_samples

        for iChannel, channel in enumerate(allChannels):
            ctrlprm = channel.ctrlprm_struct.payload
            # TODO: where are RADAR_NBEAMS and RADAR_BEAMWIDTH is comming from?
#            bmazm         = calc_beam_azm_rad(RADAR_NBEAMS, ctrlprm['tbeam'], RADAR_BEAMWIDTH)    # calculate beam azimuth from transmit beam number
            # TODO: use data from config file
            beam_sep  = float(self.ini_array_settings['beam_sep'] ) # degrees
            nbeams    = int(  self.ini_array_settings['nbeams'] )
            x_spacing = float(self.ini_array_settings['x_spacing'] ) # meters 
            beamnum   = ctrlprm['tbeam']

            bmazm         = calc_beam_azm_rad(nbeams, beamnum, beam_sep)    # calculate beam azimuth from transmit beam number          
            pshift        = calc_phase_increment(bmazm, ctrlprm['tfreq'] * 1000., x_spacing)       # calculate antenna-to-antenna phase shift for steering at a frequency        
            antennas_list = [0]   # TODO: HARDCODED TO ONE ANTENNA
            phasingMatrix_main = np.array([rad_to_rect(a * pshift) for a in antennas_list])  # calculate a complex number representing the phase shift for each antenna
           #phasingMatrix_back = np.ones(len(MAIN_ANTENNAS))


            channel.main_beamformed = _beamform_uhd_samples(main_samples[iChannel], phasingMatrix_main, nSamples_bb, antennas_list)
           #channel.back_beamformed = _beamform_uhd_samples(back_samples, phasingMatrix_back, nSamples_bb, antennas)
        
        cprint('get_data complete!', 'blue')



    def deleteRadarChannel(self, channelObject):
        if channelObject in self.channels:
            # don't delete channel in middle of trigger, pretrigger, or ....
            channelObject._waitForState(STATE_WAIT) 
            print('RHM:deleteRadarChannel removing channel {} from HardwareManager'.format(self.channels.index(channelObject)))
            self.channels.remove(channelObject)
            print('RHM:deleteRadarChannel {} channels left'.format(len(self.channels)))
            if (len(self.channels) == 0) and not self.addingNewChannelsAllowed:  # reset flag for adding new channels  if last channel has been deleted between PRETRIGGER and GET_DATA
                self.addingNewChannelsAllowed = True

            self.nRegisteredChannels -= 1
            if self.nRegisteredChannels == 0:  
                self.commonChannelParameter = {}

        else:
            print('RHM:deleteRadarChannel channel already deleted')


        

    def exit(self):
        # clean up and exit
        self.client_sock.close()
        cmd = usrp_exit_command(self.usrpsocks)
        cmd.transmit()

        cmd = cuda_exit_command(self.cudasocks)
        cmd.transmit()

        for sock in self.usrpsocks:
            sock.close()
        for sock in self.cudasocks:
            sock.close()

        cprint('received exit command, cleaning up..', 'red')

        sys.exit(0)

    def trigger(self):
        cprint('running trigger, triggering usrp drivers', 'yellow')

        if len(self.channels):
        
            # grab current time from a usrp
            cmd = usrp_get_time_command(self.usrpsocks[0])
            cmd.transmit() 
            usrp_time = cmd.recv_time(self.usrpsocks[0])
            cmd.client_return()
            trigger_time = usrp_time + USRP_TRIGGER_START_DELAY
            cmd = usrp_trigger_pulse_command(self.usrpsocks, trigger_time)
            cprint('sending trigger pulse command', 'yellow')
            cmd.transmit()
            # TODO: handle multiple usrps with trigger..
            cprint('waiting for trigger return', 'yellow')
            returns = cmd.client_return()

            if TRIGGER_BUSY in returns:
                cprint('could not trigger, usrp driver is busy', 'yellow')
                pdb.set_trace()

            cprint('trigger return!', 'yellow')

        else:
            cprint('no channels loaded, trigger failed!', 'green')
            pdb.set_trace()

        for ch in self.channels:
            ch.state = STATE_WAIT

        cprint('trigger complete', 'yellow')

    # TODO: Merge channel infomation here!
    # TODO: pretrigger cuda client return fails
    # key error: 0?
    def pretrigger(self):
        cprint('running RadarHardwareManager.pretrigger()', 'blue')
        

       #  cprint('running cuda_pulse_init command', 'blue')
        # TODO: untangle cuda pulse init handler
        #        pdb.set_trace()
        #        cmd = cuda_pulse_init_command(self.cudasocks)
        #        cmd.transmit()
        #        cprint('waiting for cuda driver return', 'blue')
        #        cmd.client_return()
        #        cprint('cuda return received', 'blue')

        
        
        synth_pulse = False
        print('self.channels len : ' + str(len(self.channels)))
        
        for ch in self.channels:
            #pdb.set_trace()
            if ch.ctrlprm_struct.payload['tfreq'] != 0:
                # check that we are actually able to transmit at that frequency given the USRP center frequency and sampling rate
                print('RadarHardwareManager.pretrigger() tfreq: ' + str(ch.ctrlprm_struct.payload['tfreq']))
                print('RadarHardwareManager.pretrigger() rfreq: ' + str(ch.ctrlprm_struct.payload['rfreq']))
                print('RadarHardwareManager.pretrigger() usrp tx cfreq: ' + str(self.usrp_tx_cfreq))
                print('RadarHardwareManager.pretrigger() usrp rf tx rate: ' + str(self.usrp_rf_tx_rate))
                
                assert np.abs((ch.ctrlprm_struct.payload['tfreq'] * 1e3) - self.usrp_tx_cfreq) < (self.usrp_rf_tx_rate / 2), 'transmit frequency outside range supported by sampling rate and center frequency'

                # load sequence to cuda driver if it has a nonzero transmit frequency..
                #time.sleep(.05) # TODO: investigate race condition.. why does adding a sleep here help
                #print('after 50 ms of delay..')
                #print('tfreq: ' + str(ch.ctrlprm_struct.payload['tfreq']))
                #print('rfreq: ' + str(ch.ctrlprm_struct.payload['rfreq']))

                if not (ch.ctrlprm_struct.payload['tfreq'] == ch.ctrlprm_struct.payload['rfreq']):
                    cprint('tfreq != rfreq!', 'yellow')
                    #pdb.set_trace()
                cmd = cuda_add_channel_command(self.cudasocks, sequence=ch.getSequence())
                # TODO: separate loading sequences from generating baseband samples..

                cprint('sending CUDA_ADD_CHANNEL', 'blue')
                cmd.transmit()

                cprint('waiting for CUDA_ADD_CHANNEL to return', 'blue')
                cmd.client_return()
                synth_pulse = True


        if synth_pulse:
            cprint('sending CUDA_GENERATE_PULSE', 'blue')
            cmd = cuda_generate_pulse_command(self.cudasocks)
            cmd.transmit()
            cmd.client_return()
            cprint('finished CUDA_GENERATE_PULSE', 'blue')

        # TODO: handle channels with different pulse infomation..
        # TODO: parse tx sample rate dynamocially
        # TODO: handle pretrigger with no channels
        # rfrate = np.int32(self.cuda_config['FSampTX'])
        # extract sampling info from cuda driver
        # print('rf rate parsed from ini: ' + str(rfrate))
        pulse_offsets_vector = self.commonChannelParameter['pulse_offsets_vector'] 
        npulses              = self.commonChannelParameter['npulses']
        tx_time              = self.commonChannelParameter['tx_time']
        # calculate the number of RF transmit and receive samples 
        num_requested_rx_samples = np.uint64(np.round((self.usrp_rf_rx_rate) * (self.commonChannelParameter['number_of_samples'] / self.commonChannelParameter['baseband_samplerate'])))  # TODO: might be not the exact number, compare with cuda!
        num_requested_tx_samples = np.uint64(np.round((self.usrp_rf_tx_rate)  * tx_time / 1e6))

        cprint('sending USRP_SETUP', 'blue')
        cmd = usrp_setup_command(self.usrpsocks, self.usrp_tx_cfreq, self.usrp_rx_cfreq, self.usrp_rf_tx_rate, self.usrp_rf_rx_rate, npulses, num_requested_rx_samples, num_requested_tx_samples, pulse_offsets_vector)
        cmd.transmit()
        cmd.client_return()

        for ch in self.channels:
            if ch.state == STATE_PRETRIGGER: # only reset PRETRIGGER , not TRIGGER
                ch.state = STATE_WAIT

        cprint('end of RadarHardwareManager.pretrigger()', 'blue')


class RadarChannelHandler:
    def __init__(self, conn, parent_RadarHardwareManager):
        self.active = False# TODO: eliminate
        self.ready = False # TODO: eliminate
        self.conn = conn
        self.parent_RadarHardwareManager = parent_RadarHardwareManager

        self.state = STATE_WAIT

        self.ctrlprm_struct = ctrlprm_struct(self.conn)
        self.seqprm_struct = seqprm_struct(self.conn)
        self.clrfreq_struct = clrfreqprm_struct(self.conn)
        self.rprm_struct = rprm_struct(self.conn)
        self.dataprm_struct = dataprm_struct(self.conn)

        self.clrfreq_start = 0
        self.clrfreq_stop = 0
        self.cnum = 'unknown'

        # TODO: initialize ctlrprm_struct with site infomation
        #self.ctrlprm_struct.set_data('rbeamwidth', 3)
        #self.ctrlprm_struct.set_data('tbeamwidth', 3)


    def run(self):
        rmsg_handlers = {\
            SET_RADAR_CHAN : self.SetRadarChanHandler,\
            SET_INACTIVE : self.SetInactiveHandler,\
            SET_ACTIVE : self.SetActiveHandler,\
            QUERY_INI_SETTINGS : self.QueryIniSettingsHandler,\
        #   GET_SITE_SETTINGS : self.GetSiteSettingsHandler, \
        #   UPDATE_SITE_SETTINGS : self.UpdateSiteSettingsHandler,\
            GET_PARAMETERS : self.GetParametersHandler,\
            SET_PARAMETERS : self.SetParametersHandler,\
            PING : self.PingHandler,\
        #   OKAY : self.OkayHandler,\
        #   NOOP : self.NoopHandler,\
            QUIT : self.QuitHandler,\
            REGISTER_SEQ: self.RegisterSeqHandler,\
        #   REMOVE_SEQ: self.RemoveSeqHandler,\
            REQUEST_ASSIGNED_FREQ: self.RequestAssignedFreqHandler,\
            REQUEST_CLEAR_FREQ_SEARCH: self.RequestClearFreqSearchHandler,\
            LINK_RADAR_CHAN : self.LinkRadarChanHandler,\
            SET_READY_FLAG: self.SetReadyFlagHandler,\
            UNSET_READY_FLAG: self.UnsetReadyFlagHandler,\
        #   SET_PROCESSING_FLAG: self.SetProcessingFlagHandler,\
        #   UNSET_PROCESSING_FLAG: self.UnsetProcessingFlagHandler,\
        #   WAIT_FOR_DATA: self.WaitForDataHandler,\
            GET_DATA: self.GetDataHandler}

        while not self.parent_RadarHardwareManager.addingNewChannelsAllowed:
            print("Waiting to finish GET_DATA before adding new channel")
            time.sleep(RADAR_STATE_TIME)

        # adding channel to HardwareManager
        self.parent_RadarHardwareManager.channels.append(self)  

        while True:
            rmsg = rosmsg_command(self.conn)
            status = RMSG_FAILURE

            print('RadarChannelHandler (ch {}): waiting for command'.format(self.cnum))
            rmsg.receive(self.conn)
            command = chr(rmsg.payload['type'] & 0xFF) # for some reason, libtst is sending out 4 byte commands with junk..
            try:
                cprint('RadarChannelHandler (ch {}): received command (ROS=>USRP_Server): {}, {}'.format(self.cnum, command, RMSG_COMMAND_NAMES[command]), 'red')
            except KeyError:
                print('unrecognized command!')
                print(rmsg.payload)
                pdb.set_trace()

            if command in rmsg_handlers:
                status = rmsg_handlers[command](rmsg)
            else:
                status = self.DefaultHandler(rmsg)

            if status == 'exit': # output of QuitHandler
                break
 
            rmsg.set_data('status', status)
            rmsg.set_data('type', rmsg.payload['type'])
            rmsg.transmit()


    def close(self):
        print('todo: write close..:')
        pdb.set_trace()
        # TODO write this..


    # busy wait until state enters desired state
    # useful for waiting for
    def _waitForState(self, state):
        wait_start = time.time()

        while self.state != state:
            #cprint('channel {} waiting for {}, current state:'.format(self.cnum, self.state, state), 'green')
            time.sleep(RADAR_STATE_TIME)
            if time.time() - wait_start > CHANNEL_STATE_TIMEOUT:
                print('CHANNEL STATE TIMEOUT')
                pdb.set_trace()
                break

    # return a sequence object, used for passing pulse sequence and channel infomation over to the CUDA driver
    def getSequence(self):
        return sequence(self.npulses, self.tr_to_pulse_delay, self.pulse_offsets_vector, self.pulse_lens, self.phase_masks, self.pulse_masks, self.ctrlprm_struct.payload)

    def DefaultHandler(self, rmsg):
        print("Unexpected command: {}".format(chr(rmsg.payload['type'])))
        pdb.set_trace()
        return RMSG_FAILURE

    def QuitHandler(self, rmsg):
        # TODO: close down stuff cleanly
        rmsg.set_data('status', RMSG_FAILURE)
        rmsg.set_data('type', rmsg.payload['type'])
        rmsg.transmit()
        self.conn.close()
        self.parent_RadarHardwareManager.deleteRadarChannel(self)
        del self # TODO close thread ?!?
        print('Deleted channel')
        # sys.exit() # TODO: set return value
        return 'exit'

    def PingHandler(self, rmsg):
        return RMSG_SUCCESS

    def RequestAssignedFreqHandler(self, rmsg):
        # wait for clear frequency search to end, hardware manager will set channel state to WAIT
        self._waitForState(STATE_WAIT) 

        transmit_dtype(self.conn, self.tfreq, np.int32)
        transmit_dtype(self.conn, self.noise, np.float32)

        self.clrfreq_stop = time.time()
        cprint('clr frequency search time: {}'.format(self.clrfreq_stop - self.clrfreq_start), 'yellow')
        return RMSG_SUCCESS

    def RequestClearFreqSearchHandler(self, rmsg):
        self._waitForState(STATE_WAIT)
        self.clrfreq_struct.receive(self.conn)
        self.clrfreq_start = time.time()

        # request clear frequency search time from hardware manager
        self.state = STATE_CLR_FREQ

        return RMSG_SUCCESS

    def UnsetReadyFlagHandler(self, rmsg):
        self.ready = False
        return RMSG_SUCCESS


    def SetReadyFlagHandler(self, rmsg):
        self._waitForState(STATE_WAIT)
        # TODO: check if samples are ready?
        self.state = STATE_TRIGGER
        # send trigger command
        self.ready = True
        self._waitForState(STATE_WAIT)
        return RMSG_SUCCESS

    def RegisterSeqHandler(self, rmsg):
        # function to get the indexes of rising edges going from zero to a nonzero value in array ar
        def _rising_edge_idx(ar):
            ar = np.insert(ar, 0, -2)
            edges = np.array([ar[i+1] * (ar[i+1] - ar[i] > 1) for i in range(len(ar)-1)])
            return edges[edges > 0]

        # returns the run length of a pulse in array ar starting at index idx
        def _pulse_len(ar, idx):
            runlen = 0
            for element in ar[idx:]:
                if not element:
                    break
                runlen += 1
            return runlen
        # site libraries appear to not initialize the status, so a nonzero status here is normall.

        self.seqprm_struct.receive(self.conn)
        self.seq_rep = recv_dtype(self.conn, np.uint8, self.seqprm_struct.payload['len'])
        self.seq_code = recv_dtype(self.conn, np.uint8, self.seqprm_struct.payload['len'])

        tx_tsg_idx = self.seqprm_struct.get_data('index')
        tx_tsg_len = self.seqprm_struct.get_data('len')
        tx_tsg_step = self.seqprm_struct.get_data('step')
        
        # ratio between tsg step (units of microseconds) to baseband sampling period
        # TODO: calculate this from TXUpsampleRate, FSampTX in cuda_config.ini
        # however, it should always be 1..
        tsg_bb_per_step = 1

        # psuedo-run length encoded tsg
        tx_tsg_rep = self.seq_rep
        tx_tsg_code = self.seq_code

        seq_buf = []
        for i in range(tx_tsg_len):
            for j in range(0, np.int32(tsg_bb_per_step * tx_tsg_rep[i])):
                seq_buf.append(tx_tsg_code[i])
        seq_buf = np.uint8(np.array(seq_buf))

        # extract out pulse information...
        S_BIT = np.uint8(0x01) # sample impulses
        R_BIT = np.uint8(0x02) # tr gate, use for tx pulse times
        X_BIT = np.uint8(0x04) # transmit path, use for bb
        A_BIT = np.uint8(0x08) # enable attenuator
        P_BIT = np.uint8(0x10) # phase code (BPSK)

        # create masks
        samples = seq_buf & S_BIT
        tr_window = seq_buf & R_BIT
        rf_pulse = seq_buf & X_BIT
        atten = seq_buf & A_BIT
        phase_mask = seq_buf & P_BIT

        # extract and number of samples
        sample_idx = np.nonzero(samples)[0]
        assert len(sample_idx) > 3, 'register empty sequence'

        nbb_samples = len(sample_idx)

        # extract pulse start timing
        tr_window_idx = np.nonzero(tr_window)[0]
        tr_rising_edge_idx = _rising_edge_idx(tr_window_idx)
        pulse_offsets_vector = tr_rising_edge_idx * tx_tsg_step

        # extract tr window to rf pulse delay
        rf_pulse_idx = np.nonzero(rf_pulse)[0]
        rf_pulse_edge_idx = _rising_edge_idx(rf_pulse_idx)
        tr_to_pulse_delay = (rf_pulse_edge_idx[0] - tr_rising_edge_idx[0]) * tx_tsg_step
        npulses = len(rf_pulse_edge_idx)

        # extract per-pulse phase coding and transmit pulse masks
        # indexes are in microseconds from start of pulse
        phase_masks = []
        pulse_masks = []
        pulse_lens = []

        for i in range(npulses):
            pstart = rf_pulse_edge_idx[i]
            pend = pstart + _pulse_len(rf_pulse, pstart)
            phase_masks.append(phase_mask[pstart:pend])
            pulse_masks.append(rf_pulse[pstart:pend])
            pulse_lens.append((pend - pstart) * tx_tsg_step)

        self.npulses = npulses
        self.pulse_offsets_vector = pulse_offsets_vector / 1e6
        self.pulse_lens = pulse_lens # length of pulses, in seconds
        self.phase_masks = phase_masks # phase masks are complex number to multiply phase by, so
        self.pulse_masks = pulse_masks
        self.tr_to_pulse_delay = tr_to_pulse_delay
        # self.ready = True # TODO: what is ready flag for?
        self.tx_time = self.pulse_lens[0] + 2 * self.tr_to_pulse_delay

        if debug:
           print("pulse0 length: {} us, tr_pulse_delay: {} us, tx_time: {} us".format(self.pulse_lens[0], tr_to_pulse_delay, self.tx_time))
        if npulses == 0:
            raise ValueError('number of pulses must be greater than zero!')
        if nbb_samples == 0:
            raise ValueError('number of samples in sequence must be nonzero!')

      #  pdb.set_trace()

        return RMSG_SUCCESS

    # receive a ctrlprm struct
    def SetParametersHandler(self, rmsg):
        self._waitForState(STATE_WAIT)
        cprint('channel {} starting PRETRIGGER'.format(self.cnum), 'green')
        self.ctrlprm_struct.receive(self.conn)
        print('usrp_server RadarChannelHandler.SetParametersHandler: tfreq: ' + str(self.ctrlprm_struct.payload['tfreq']))
        print('usrp_server RadarChannelHandler.SetParametersHandler: rfreq: ' + str(self.ctrlprm_struct.payload['rfreq']))

        tmp = self.CheckChannelCompatibility()
        print(tmp)
        if not tmp:
#        if not self.CheckChannelCompatibility():
            print("new channel not compatible")
            return RMSG_FAILURE


        self.state = STATE_PRETRIGGER
        cprint('channel {} going into PRETRIGGER state'.format(self.cnum), 'green')
        self._waitForState(STATE_WAIT)
        # TODO for a SetParametersHandler, prepare transmit and receive sample buffers
        if (self.rnum < 0 or self.cnum < 0):
            return RMSG_FAILURE
        return RMSG_SUCCESS

    def CheckChannelCompatibility(self):
        hardwareManager = self.parent_RadarHardwareManager
        commonParList_ctrl = ['number_of_samples', 'baseband_samplerate' ]
        commonParList_seq = [ 'npulses', 'pulse_offsets_vector', 'tx_time' ]

        if hardwareManager.nRegisteredChannels == 0:  # this is the first channel
            hardwareManager.commonChannelParameter = {key: getattr(self, key) for key in commonParList_seq}
            hardwareManager.commonChannelParameter.update( {key: self.ctrlprm_struct.payload[key] for key in commonParList_ctrl})

            hardwareManager.nRegisteredChannels = 1
            return True

        else:   # not first channel => check if new parameters are compatible
            parCompatibleList_seq = [hardwareManager.commonChannelParameter[parameter] == getattr(self, parameter) for parameter in commonParList_seq]
            parCompatibleList_ctrl = [hardwareManager.commonChannelParameter[parameter] == self.ctrlprm_struct.payload[parameter] for parameter in commonParList_ctrl]
            idxOffsetVec = commonParList_seq.index('pulse_offsets_vector')  # convert vector of bool to scalar
            parCompatibleList_seq[idxOffsetVec] = parCompatibleList_seq[idxOffsetVec].all()
 
         #   pdb.set_trace()
            if (not all(parCompatibleList_seq)) or (not  all(parCompatibleList_ctrl)):
                print('Unable to add new channel. Parameters not compatible with active channels.')
                for iPar,isCompatible in enumerate(parCompatibleList_seq):
                     if not all(isCompatible):
                        print(" Not compatible sequence parameter: {}   old channel(s): {} , new channel: {}".format(commonParameterList_seq[iPar], hardwareManager.commonChannelParameter[commonParameterList_seq[iPar]] , getattr(self, commonParameterList_seq[iPar])))
                for iPar,isCompatible in enumerate(parCompatibleList_ctrl):
                     if not isCompatible:
                        print(" Not compatible ctrlprm: {}   old channel(s): {} , new channel: {}".format(commonParameterList_ctrl[iPar], hardwareManager.commonChannelParameter[commonParameterList_ctrl[iPar]] , self.ctrlprm_struct.payload[commonParameterList_ctrl[iPar]]))
                return False
            else:
                hardwareManager.nRegisteredChannels += 1
                return True
                    

        
     


    # send ctrlprm struct
    def GetParametersHandler(self, rmsg):
        # TODO: return bad status if negative radar or channel
        self.ctrlprm_struct.transmit()
        return RMSG_SUCCESS

    def GetDataHandler(self, rmsg):
        cprint('entering hardware manager get data handler', 'blue')
        self.dataprm_struct.set_data('samples', self.ctrlprm_struct.payload['number_of_samples'])

        self.dataprm_struct.transmit()
        cprint('sending dprm struct', 'green')

        if not self.active or self.rnum < 0 or self.cnum < 0:
            pdb.set_trace()
            return RMSG_FAILURE

        # TODO investigate possible race conditions
        cprint('waiting for channel to idle before GET_DATA', 'blue')
        self._waitForState(STATE_WAIT)

        cprint('entering GET_DATA state', 'blue')
        self.state = STATE_GET_DATA
        cprint('waiting to return to WAIT before returning samples', 'blue')
        self._waitForState(STATE_WAIT)
        cprint('GET_DATA complete, returning samples', 'blue')


        # TODO: get data handler waits for control_program to set active flag in controlprg struct
        # need some sort of synchronizaion..
        # TODO: back samples..
        main_samples = self.main_beamformed
        back_samples = np.zeros(self.dataprm_struct.get_data('samples'))

        transmit_dtype(self.conn, main_samples, np.uint32)
        transmit_dtype(self.conn, back_samples, np.uint32)
        

        badtrdat_start_usec = self.pulse_offsets_vector * 1e6 # convert to us
        transmit_dtype(self.conn, self.npulses, np.uint32)
        transmit_dtype(self.conn, badtrdat_start_usec, np.uint32) # length badtrdat_len
        transmit_dtype(self.conn, self.pulse_lens, np.uint32) # length badtrdat_len

        # stuff these with junk, they don't seem to be used..
        num_transmitters = 16 
        txstatus_agc = np.zeros(num_transmitters)
        txstatus_lowpwr = np.zeros(num_transmitters)

        transmit_dtype(self.conn, num_transmitters, np.int32)
        transmit_dtype(self.conn, txstatus_agc, np.int32) # length num_transmitters
        transmit_dtype(self.conn, txstatus_lowpwr, np.int32) # length num_transmitters

        return RMSG_SUCCESS

    def SetRadarChanHandler(self, rmsg):
        self.rnum = recv_dtype(self.conn, np.int32)
        self.cnum = recv_dtype(self.conn, np.int32)
        
        self.ctrlprm_struct.set_data('channel', self.cnum)
        self.ctrlprm_struct.set_data('radar',  self.rnum)

        # TODO: how to handle channel contention?
        if(debug):
            print('radar num: {}, radar chan: {}'.format(self.rnum, self.cnum))

        # TODO: set RMSG_FAILURE if radar channel is unavailable
        # rmsg.set_data('status', RMSG_FAILURE)
        return RMSG_SUCCESS

    def LinkRadarChanHandler(self, rmsg):
        rnum = recv_dtype(self.conn, np.int32)
        cnum = recv_dtype(self.conn, np.int32)
        print('link radar chan is unimplemented!')
        pdb.set_trace()
        return RMSG_SUCCESS


    def QueryIniSettingsHandler(self, rmsg):
        # TODO: don't hardcode this if I find anything other than ifmode querying..
        data_length = recv_dtype(self.conn, np.int32)
        ini_name = recv_dtype(self.conn, str, nitems=data_length)
        requested_type = recv_dtype(self.conn, np.uint8)

        # hardcode to reply with ifmode is false
        assert ini_name == b'site_settings:ifmode\x00'

        payload = 0 # assume always false

        transmit_dtype(self.conn, requested_type, np.uint8)
        transmit_dtype(self.conn, data_length, np.int32) # appears to be unused by site library
        transmit_dtype(self.conn, payload, np.int32)

        return 1 # TODO: Why does the ini handler expect a nonzero response for success?

    def SetActiveHandler(self, rmsg):
        # TODO: return failure if rnum and cnum are not set
        # TODO: why is active only set if it is already set?
        if not self.active:
            self.active = True
            self.ready = False
        return RMSG_SUCCESS

    def SetInactiveHandler(self, rmsg):
        # TODO: return failure status if the radar or channel number is invalid

        if not self.active:
            self.active = False
            self.ready = False
            # TODO: what is coordination handler doing?

        return RMSG_SUCCESS


def main():
    # maybe switch to multiprocessing with manager process
    print('main!')
    logging.basicConfig(filename='client_server.log', level=logging.DEBUG, format='[%(levelname)s] (%(threadName)-10s) %(asctime)s %(message)s',)

    logging.debug('main()')
    rmsg_port = 45000

    radar = RadarHardwareManager(rmsg_port)
    radar.run()


if __name__ == '__main__':
    main()

