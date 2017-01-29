#!/usr/bin/python3
# test the usrp driver..
import unittest
import numpy as np
import sys
import posix_ipc
import pdb
import time
import subprocess
import configparser


from termcolor import cprint

sys.path.insert(0, '../python_include')
sys.path.insert(0, '../cuda_driver')

from drivermsg_library import *
from socket_utils import *
from cuda_driver import *
from radar_config_constants import *


START_DRIVER = False
ANTENNA_UNDER_TEST = 0


# parse gpu config file
driverconfig = configparser.ConfigParser()
driverconfig.read('../driver_config.ini')
shm_settings = driverconfig['shm_settings']
cuda_settings = driverconfig['cuda_settings']
network_settings = driverconfig['network_settings']

rxshm_size = shm_settings.getint('rxshm_size')
txshm_size = shm_settings.getint('txshm_size')


USRPDRIVER_PORT = network_settings.getint('USRPDriverPort')

RFRATE = 10000000


rx_shm_list = [[],[]]
tx_shm_list = []
rx_semaphore_list = [[],[]] # [side][swing]
swings = [SWING0, SWING1]
sides = [SIDEA]

# list of all semaphores/shared memory paths for cleaning up
shm_list = []
sem_list = []
             

if sys.hexversion < 0x030300F0:
    print('this code requires python 3.3 or greater')
    sys.exit(0)

def start_usrpserver():
    # open up ports..
    if not START_DRIVER:
        return -1
    usrp_driver = subprocess.Popen(['./usrp_driver', '--intclk', '--antenna', str(ANTENNA_UNDER_TEST), '--host', 'usrp' + str(ANTENNA_UNDER_TEST)])
    time.sleep(8)
    return usrp_driver.pid

def stop_usrpserver(sock, pid):
    # transmit clean exit command
    exitcmd = usrp_exit_command([sock])
    exitcmd.transmit()
    print('stopping USRP server')

    # kill the process just to be sure..
    if not START_DRIVER:
        return
    
    print('killing usrp_driver')
    subprocess.Popen(['pkill', 'usrp_driver'])
    time.sleep(10) # delay to let the usrp_driver settle..
    #pdb.set_trace()
    
class USRP_ServerTestCases(unittest.TestCase):
    def setUp(self):
        antennas = [ANTENNA_UNDER_TEST]
        for ant in antennas:
            rx_shm_list[SIDEA].append(create_shm(ant, SWING0, SIDEA, rxshm_size, direction = RXDIR))
            rx_shm_list[SIDEA].append(create_shm(ant, SWING1, SIDEA, rxshm_size, direction = RXDIR))
            tx_shm_list.append(create_shm(ant, SWING0, SIDEA, txshm_size, direction = TXDIR))
            tx_shm_list.append(create_shm(ant, SWING1, SIDEA, txshm_size, direction = TXDIR))

        rx_semaphore_list[SIDEA].append(create_sem(ant, SWING0))
        rx_semaphore_list[SIDEA].append(create_sem(ant, SWING1))

        time.sleep(1)
        self.pid = start_usrpserver()

        self.serversock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        max_connect_attempts = 5
        for i in range(max_connect_attempts):
            print('attempting connection to usrp_driver (at localhost:{}'.format(USRPDRIVER_PORT + ANTENNA_UNDER_TEST))
            try:
                self.serversock.connect(('localhost', USRPDRIVER_PORT + ANTENNA_UNDER_TEST))
                break;
            except:
                print('connecting to usrp driver failed on attempt ' + str(i + 1))
                time.sleep(3)

                if i == (max_connect_attempts - 1):
                    subprocess.Popen(['pkill', 'usrp_driver'])
                    print('connecting to usrp driver failed, exiting')
                    sys.exit(1)

    def tearDown(self):
        for shm in shm_list:
            posix_ipc.unlink_shared_memory(shm)

        for sem in sem_list:
            sem.release()
            sem.unlink()

        #stop_usrpserver(self.serversock, self.pid)
        self.serversock.close()
    '''
    # test setting up usrp.. 
    def test_usrp_setup(self):
        cprint('usrp initialization','red')
        seq = create_testsequence()
        cmd = usrp_setup_command([self.serversock], seq.ctrlprm, seq, RFRATE)
        client_returns = cmd.client_return()
        for r in client_returns:
            assert(r == UHD_SETUP)
    
    # test reading/writing to shm
    def test_usrp_shm(self):
        cprint('testing shared memory usrp rxfe setup', 'red')
        # TODO: INITIALIZE SHARED MEMORY IN USRP_DRIVER, READ BACK HERE
        # populated first 10 spots in shm with 0,1,2,3,4,5,6,7,8,9
        shm = tx_shm_list[0]
        shm.seek(0)
        for i in range(10):
            c = shm.read_byte()
            print("checking byte {} of shm, read {}".format(i, c))
            assert(i == c)

    # test rxfe setup
    def test_usrp_rxfe(self):
        cprint('testing usrp rxfe setup', 'red')
        cmd = usrp_rxfe_setup_command([self.serversock], 0, 0, 0)
        cmd.transmit()
        client_returns = cmd.client_return()
        for r in client_returns:
            assert(r == UHD_RXFE_SET)


        time.sleep(5)
        cmd = usrp_rxfe_setup_command([self.serversock], 1, 1, 31)
        cmd.transmit()
        client_returns = cmd.client_return()
        for r in client_returns:
            assert(r == UHD_RXFE_SET)

        time.sleep(5)
    '''
    def fill_tx_shm_with_one_pulse(self, seq):
        cprint('populating tx shm sample buffer','red')
        tx_shm = tx_shm_list[0]
        tx_shm.seek(0)

        # create test tone
        tone = 50e3 # 50 khz tone..
        amplitude = np.iinfo(np.int16).max / 2 # 1/2 of max amplitude
        print("nPulses: {}, tx_time: {}, RFRATE: {}".format(seq.npulses, seq.tx_time, RFRATE))
        samplet = np.arange(0,seq.npulses * seq.tx_time/1e6 ,1/RFRATE)#[:-1]
        sample_real = np.int16(amplitude * np.cos(2 * np.pi * tone * samplet))
        sample_imag = np.int16(amplitude * np.sin(2 * np.pi * tone * samplet))
        sample_tx = np.zeros(2 * len(samplet), dtype=np.int16)
        nSamples_per_pulse =  (seq.tx_time/1e6*RFRATE)

        sample_tx[0::2] = sample_real
        sample_tx[1::2] = sample_imag
        tx_shm.write(sample_tx.tobytes())

        return nSamples_per_pulse
    def test_trigger_pulse_one_period(self):
        cprint('testing usrp trigger with one period','red')
        seq = create_testsequence()
        nSamples_per_pulse =  self.fill_tx_shm_with_one_pulse(seq)

        # copied from usrp_driver and adjusted
        # determine the length of integration periods for all channels in seconds
        integration_period = 1 
        PULSE_SEQUENCE_PADDING_TIME = 35e3 * 75 * 2 / 3e8 # without offset
        INTEGRATION_PERIOD_SYNC_TIME = 0.2 # todo: get from file

        tx_sample_rate = RFRATE

        nPulses_in_sequence = seq.npulses
 
        # to find out how much time is available in an integration period for pulse sequences, subtract out startup delay
        sampling_duration = integration_period - INTEGRATION_PERIOD_SYNC_TIME

        # calculate the pulse sequence period with padding
        time_sequence     = PULSE_SEQUENCE_PADDING_TIME + seq.pulse_offsets_vector[-1] + seq.pulse_lens[-1] /1e6 
        nSamples_sequence = int(np.round(time_sequence * tx_sample_rate))

        # calculate the number of pulse sequences that fit in the available time within an integration period
        nSequences_in_period = int(np.floor(sampling_duration / time_sequence))

        # then calculate sample indicies at which pulse sequences start within a pulse sequence
        pulse_sequence_offsets_samples = [int(offset* tx_sample_rate) for offset in seq.pulse_offsets_vector]  
        pulse_sequence_offsets_vector = seq.pulse_offsets_vector 

        # then, calculate sample indicies at which pulses start within an integration period
        nPulses_per_period = int(nPulses_in_sequence * nSequences_in_period )
        integration_period_pulse_sample_offsets = np.zeros(nPulses_per_period, dtype=np.uint64)
        for iSequence in range(nSequences_in_period):
            for iPulse in range(nPulses_in_sequence):
                integration_period_pulse_sample_offsets[iSequence * nPulses_in_sequence + iPulse] = iSequence * nSamples_sequence + pulse_sequence_offsets_samples[iPulse]

        # calculate the number of RF transmit and receive samples 
        ctrlprm = seq.ctrlprm
        nSamples_rx = nSamples_sequence * nSequences_in_period
       
        cprint('sending setup command', 'blue')
        print("nSamples_rx:{}, nSamples_per_pulse:{}, integration_period_pulse_sample_offsets:".format(nSamples_rx, nSamples_per_pulse))
        print("nSequences_in_period:{}, nPulses_per_period:{}, ".format(nSequences_in_period, nPulses_per_period))
        print(integration_period_pulse_sample_offsets)
       
        
        cmd = usrp_setup_command([self.serversock], seq.ctrlprm['tfreq'], seq.ctrlprm['rfreq'],RFRATE, RFRATE, nPulses_per_period, nSamples_rx, nSamples_per_pulse, integration_period_pulse_sample_offsets)
        cmd.transmit()
        client_returns = cmd.client_return()
        for r in client_returns:
            assert(r == UHD_SETUP)

    
        for i in range(1):
#        while True:
            # grab current usrp time from one usrp_driver
            cmd = usrp_get_time_command(self.serversock)
            cmd.transmit()
            usrp_time = cmd.recv_time(self.serversock)
            cmd.client_return()

            cprint('sending trigger pulse command', 'blue')
            trigger_time = usrp_time +  INTEGRATION_PERIOD_SYNC_TIME
            cmd = usrp_trigger_pulse_command([self.serversock], trigger_time)
            cmd.transmit()
            client_returns = cmd.client_return()
            for r in client_returns:
                assert(r == UHD_TRIGGER_PULSE) 


            cprint('checking trigger pulse data', 'blue')
            # request pulse data
            cmd = usrp_ready_data_command([self.serversock])
            cmd.transmit()
            ret = cmd.recv_metadata(self.serversock)
            print("  recieved READY STATUS: status:{}, ant: {}, nSamples: {}, fault: {}".format(ret['status'], ret['antenna'], ret['nsamples'], ret['fault']))

            client_returns = cmd.client_return()
            for r in client_returns:
                assert(r == UHD_READY_DATA) 

            cprint('finished test trigger pulse', 'green')
            
        # plot data
        rx_shm = rx_shm_list[0][0]
        rx_shm.seek(0)
        ar = np.frombuffer(rx_shm, dtype=np.int16, count=nSamples_rx*2)
        arp = np.sqrt(np.float32(ar[0::2]) ** 2 + np.float32(ar[1::2]) ** 2)
        print('sampled power')
        print(arp[:200000:1000])

        print('sampled phase')
        import matplotlib.pyplot as plt
        plt.plot(ar[::2])
        plt.plot(ar[1::2])
        plt.show()
#        pdb.set_trace() 




    # test trigger pulse read
    def xtest_trigger_pulse_one_sequence(self):
        cprint('testing usrp trigger with one sequence','red')
        seq = create_testsequence()

        nSamples_per_pulse =  self.fill_tx_shm_with_one_pulse(seq)

        nSamples_rx = np.uint64(np.round((RFRATE) * (seq.ctrlprm['number_of_samples'] / seq.ctrlprm['baseband_samplerate'])))  # TODO: thiss has to changed for integration period 

        cprint('sending setup command', 'blue')
        offset_sample_list = [offset * RFRATE for offset in seq.pulse_offsets_vector]
        cmd = usrp_setup_command([self.serversock], seq.ctrlprm['tfreq'], seq.ctrlprm['rfreq'],RFRATE, RFRATE, seq.npulses, nSamples_rx, nSamples_per_pulse, offset_sample_list)
        cmd.transmit()
        client_returns = cmd.client_return()
        for r in client_returns:
            assert(r == UHD_SETUP)

    
        for i in range(10):
#        while True:
            # grab current usrp time from one usrp_driver
            cmd = usrp_get_time_command(self.serversock)
            cmd.transmit()
            usrp_time = cmd.recv_time(self.serversock)
            cmd.client_return()

            cprint('sending trigger pulse command', 'blue')
            trigger_time = usrp_time +  INTEGRATION_PERIOD_SYNC_TIME
            cmd = usrp_trigger_pulse_command([self.serversock], trigger_time)
            cmd.transmit()
            client_returns = cmd.client_return()
            for r in client_returns:
                assert(r == UHD_TRIGGER_PULSE) 


            cprint('checking trigger pulse data', 'blue')
            # request pulse data
            cmd = usrp_ready_data_command([self.serversock])
            cmd.transmit()
            ret = cmd.recv_metadata(self.serversock)
            print("  recieved READY STATUS: status:{}, ant: {}, nSamples: {}, fault: {}".format(ret['status'], ret['antenna'], ret['nsamples'], ret['fault']))

            client_returns = cmd.client_return()
            for r in client_returns:
                assert(r == UHD_READY_DATA) 

            cprint('finished test trigger pulse', 'green')
            
        # plot data
        num_rx_samples = np.uint64(2*np.round((RFRATE) * (seq.ctrlprm['number_of_samples'] / seq.ctrlprm['baseband_samplerate'])))
        rx_shm = rx_shm_list[0][0]
        rx_shm.seek(0)
        ar = np.frombuffer(rx_shm, dtype=np.int16, count=num_rx_samples)
        arp = np.sqrt(np.float32(ar[0::2]) ** 2 + np.float32(ar[1::2]) ** 2)
        print('sampled power')
        print(arp[:200000:1000])

        print('sampled phase')
        import matplotlib.pyplot as plt
#       plt.plot(ar[::2])
#       plt.plot(ar[1::2])
#       plt.show()
#        pdb.set_trace() 

        

    '''
    # test clear frequency
    def test_usrp_clrfreq(self):
        pass

    def test_ready_data_process(self):
        pass
         
    '''  
if __name__ == '__main__':
    make = subprocess.call(['make'])
    time.sleep(.5)

    unittest.main()
