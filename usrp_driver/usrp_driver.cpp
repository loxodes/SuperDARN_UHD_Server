// usrp_driver
// connect to a USRP using UHD
// wait for connection from usrp_server
// 
// driver listens on port: base_port + x
//   where base_port is defined in driver_config.ini and 
//   x is 3rd part of usrp ip (192.168.x.2)
//
// look at boost property tree ini parser for usrp_config.ini..
// 
//
#include <string.h>
#include <unistd.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/mman.h>
#include <sys/types.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <signal.h>
#include <iostream>
#include <complex>
#include <vector>
#include <semaphore.h>
#include <time.h>
#include <fcntl.h>

#include <argtable2.h>
#include <uhd/types/tune_request.hpp>
#include <uhd/utils/thread_priority.hpp>
#include <uhd/utils/safe_main.hpp>
#include <uhd/usrp/multi_usrp.hpp>
#include <uhd/transport/udp_simple.hpp>
#include <uhd/exception.hpp>
#include <uhd/utils/msg.hpp>

#include <boost/algorithm/string.hpp>
#include <boost/program_options.hpp>
#include <boost/property_tree/ptree.hpp>
#include <boost/property_tree/ini_parser.hpp>
#include <boost/format.hpp>
#include <boost/thread.hpp>
#include <boost/cstdint.hpp>

#include "usrp_utils.h"
#include "tx_worker.h"
#include "rx_worker.h"
#include "dio.h"

#define SAVE_RAW_SAMPLES_DEBUG 0 
#define SUPRESS_UHD_PRINTS 0
#define DEBUG 1

#ifdef DEBUG
#define DEBUG_PRINT(...) do{ fprintf( stdout, __VA_ARGS__ ); } while( false )
#else
#define DEBUG_PRINT(...) do{ } while ( false )
#endif

#define SETUP_WAIT 1 // in seconds
// DEL
//#define SWING0 0
//#define SWING1 1
//#define NSIDES 2 // sides are halves of the usrp x300, eg. rxa and txa slots form side a
//#define VERBOSE 1

#define nSwings 2 // swings are slots in the swing buffer


#define MAX_SOCKET_RETRYS 5
#define TRIGGER_BUSY 'b'
#define SETUP_READY 'y'
#define TRIGGER_PROCESS 'p'
#define ARG_MAXERRORS 10
#define MAX_TX_PULSES 10
#define MAX_PULSE_LENGTH 10000
#define USRP_SETUP 's'
#define UHD_SYNC 'S'
#define RXFE_SET 'r'
#define CLRFREQ 'c'
#define READY_DATA 'd'
#define TRIGGER_PULSE 't'
#define UHD_GETTIME 'm'
#define EXIT 'e'

#define TXDIR 1
#define RXDIR 0
#define INIT_SHM 1

#define CAPTURE_ERRORS 0

enum driver_states
{
    ST_INIT,
    ST_READY,
    ST_PULSE
};

namespace po = boost::program_options;
int32_t driversock = 0;
int32_t driverconn = 0;
int32_t verbose = 1;

void uhd_term_message_handler(uhd::msg::type_t type, const std::string &msg){
    ;
}

void *open_sample_shm(int32_t ant, int32_t dir, int32_t side, int32_t swing, size_t shm_size) {
    void *pshm = NULL;
    char shm_device[80];
    int32_t shm_fd;
    
    if(dir == TXDIR) {
        sprintf(shm_device,"/shm_tx_ant_%d_side_%d_swing_%d", ant, side, swing);
    }
    else {
        sprintf(shm_device,"/shm_rx_ant_%d_side_%d_swing_%d", ant, side, swing);
    }

    fprintf(stderr, "usrp_driver opening: %s\n", shm_device);
    shm_fd = shm_open(shm_device, O_RDWR, S_IRUSR | S_IWUSR);
    if (shm_fd == -1) {
        if (errno == ENOENT) {
       	  fprintf(stderr, "Error: shared memory name did not exist. Is cuda_driver running? (errno %d)\n", errno);
         }
        else {
           fprintf(stderr, "Couldn't get a handle to the shared memory; errno is %d\n", errno);
        }
    }
    
    if (ftruncate(shm_fd, shm_size) != 0){
        fprintf(stderr, "ftruncate error!! Shared memory buffer is undersized.\n");
    }

    pshm = mmap((void *)0, shm_size, PROT_READ | PROT_WRITE, MAP_SHARED, shm_fd, 0);
    if (pshm == MAP_FAILED) {
        fprintf(stderr, "MMapping shared memory failed; errno is %d", errno);
    }

    if(INIT_SHM) {
       int32_t i = 0;
       for(i = 0; i < 100; i++) {
            *((char *)pshm + i)= i;
       }
    }
    
    close(shm_fd);    
    return pshm;
}

sem_t open_sample_semaphore(int32_t ant, int32_t swing, int32_t dir ) {
    char sem_name[80];
    sem_t *sem = NULL;
    if(dir == TXDIR) {
       sprintf(sem_name,"/semaphore_tx_ant_%d_swing_%d", ant, swing);
    }
    else {
       sprintf(sem_name,"/semaphore_rx_ant_%d_swing_%d", ant, swing);
    }
    
    fprintf(stderr, "usrp_driver opening: %s\n", sem_name);
    sem = sem_open(sem_name, 0);

    if (sem == SEM_FAILED) {
        sem = NULL;
        fprintf(stderr, "Getting a handle to the semaphore failed; errno is %d", errno);
    }

    return *sem;
}

void lock_semaphore(sem_t sem)
{
    sem_wait(&sem);
}

void unlock_semaphore(sem_t sem)
{
    sem_post(&sem);

}


double sock_get_float64(int32_t sock)
{
   double d = 0;
   ssize_t status = recv(sock, &d, sizeof(double), MSG_WAITALL);
   if(status != sizeof(double)) {
        fprintf(stderr, "error receiving float64\n");
   }
   return d;
}

uint32_t sock_get_uint32(int32_t sock)
{
   uint32_t d = 0;
   ssize_t status = recv(sock, &d, sizeof(uint32_t), MSG_WAITALL);
   if(status != sizeof(uint32_t)) {
        fprintf(stderr, "error receiving uint32_t\n");
   }
   return d;
}

int32_t sock_get_int32(int32_t sock)
{
   int32_t d = 0;
   ssize_t status = recv(sock, &d, sizeof(int32_t), MSG_WAITALL);
   if(status != sizeof(int32_t)) {
        fprintf(stderr, "error receiving int32_t\n");
   }
   return d;
}

int16_t sock_get_int16(int32_t sock)
{
   int16_t d = 0;
   ssize_t status = recv(sock, &d, sizeof(int16_t), MSG_WAITALL);
   if(status != sizeof(int16_t)) {
        fprintf(stderr, "error receiving int16_t\n");
   }
   return d;
}


ssize_t sock_send_int32(int32_t sock, int32_t d)
{
   ssize_t status = send(sock, &d, sizeof(uint32_t), 0);
   if(status != sizeof(uint32_t)) {
        fprintf(stderr, "error sending int32_t\n");
   }
   return status;
}

ssize_t sock_send_cshort(int32_t sock, std::complex<short int> d)
{
   ssize_t status = send(sock, &d, sizeof(std::complex<short int>), 0);
   if(status != sizeof(std::complex<short int>)) {
        fprintf(stderr, "error sending compelx short\n");
   }
   return status;
}

ssize_t sock_send_bool(int32_t sock, bool d)
{
   ssize_t status = send(sock, &d, sizeof(bool), 0);
   if(status != sizeof(bool)) {
        fprintf(stderr, "error sending bool\n");
   }
   return status;
}

ssize_t sock_send_int16(int32_t sock, int16_t d)
{
   ssize_t status = send(sock, &d, sizeof(int16_t), 0);
   if(status != sizeof(uint16_t)) {
        fprintf(stderr, "error sending int16_t\n");
   }
   return status;
}

ssize_t sock_send_uint16(int32_t sock, uint16_t d)
{
   ssize_t status = send(sock, &d, sizeof(uint16_t), 0);
   if(status != sizeof(uint16_t)) {
        fprintf(stderr, "error sending uint16_t\n");
   }
   return status;
}



ssize_t sock_send_uint32(int32_t sock, uint32_t d)
{
   ssize_t status = send(sock, &d, sizeof(uint32_t), 0);
   if(status != sizeof(uint32_t)) {
        fprintf(stderr, "error sending uint32_t\n");
   }
   return status;
}


ssize_t sock_send_float64(int32_t sock, double d)
{
   ssize_t status = send(sock, &d, sizeof(double), 0);
   if(status != sizeof(double)) {
        fprintf(stderr, "error sending float64\n");
   }
   return status;
}


// break up a complex int16 into real and imag, send over the socket..
ssize_t sock_send_complex_int16(int32_t sock, std::complex<int16_t> d)
{
   ssize_t status;
   int16_t temp;
    
   temp = d.real();
   status  = send(sock, &temp, sizeof(int16_t), 0);

   temp = d.imag();
   status += send(sock, &temp, sizeof(int16_t), 0);

   if(status != sizeof(std::complex<int16_t>)) {
        fprintf(stderr, "error sending complex int16_t\n");
   }

   return status;
}

uint64_t sock_get_uint64(int32_t sock)
{
   uint64_t d = 0;
   ssize_t status = recv(sock, &d, sizeof(uint64_t), MSG_WAITALL);
   if(status != sizeof(uint64_t)) {
        fprintf(stderr, "error receiving uint64_t\n");
   }
   return d;
}

uint8_t sock_get_uint8(int32_t sock)
{
   uint8_t d = 0;
   ssize_t status = recv(sock, &d, sizeof(uint8_t), MSG_WAITALL);
   if(status != sizeof(uint8_t)) {
        fprintf(stderr, "err=%d: %s\n", errno, strerror(errno));
   }
   return d;
}

uint8_t sock_get_cmd(int32_t sock, ssize_t *status)
{
   uint8_t d = 0;
   *status = recv(sock, &d, sizeof(uint8_t), MSG_WAITALL);
   return d;
}



ssize_t sock_send_uint8(int8_t sock, uint8_t d)
{
   ssize_t status = send(sock, &d, sizeof(uint8_t), 0);
   if(status != sizeof(uint8_t)) {
        fprintf(stderr, "error sending uint8_t\n");
   }
   return status;
}

void siginthandler(int sigint)
{
    // probably unsafe to munmap and sem_close in signal handler..
    // rc = munmap(pSharedMemory, (size_t)params.size)
    // rc = sem_close(the_semaphore); 
    // close(driversock);
    exit(1);
}


static unsigned long long lastTotalUser, lastTotalUserLow, lastTotalSys, lastTotalIdle;

void init(){
    FILE* file = fopen("/proc/stat", "r");
    fscanf(file, "cpu %llu %llu %llu %llu", &lastTotalUser, &lastTotalUserLow,
        &lastTotalSys, &lastTotalIdle);
    fclose(file);
}

double getCurrentCPUValue(){
    double percent;
    FILE* file;
    unsigned long long totalUser, totalUserLow, totalSys, totalIdle, total;

    file = fopen("/proc/stat", "r");
    fscanf(file, "cpu %llu %llu %llu %llu", &totalUser, &totalUserLow,
        &totalSys, &totalIdle);
    fclose(file);

    if (totalUser < lastTotalUser || totalUserLow < lastTotalUserLow ||
        totalSys < lastTotalSys || totalIdle < lastTotalIdle){
        //Overflow detection. Just skip this value.
        percent = -1.0;
    }
    else{
        total = (totalUser - lastTotalUser) + (totalUserLow - lastTotalUserLow) +
            (totalSys - lastTotalSys);
        percent = total;
        total += (totalIdle - lastTotalIdle);
        percent /= total;
        percent *= 100;
    }

    lastTotalUser = totalUser;
    lastTotalUserLow = totalUserLow;
    lastTotalSys = totalSys;
    lastTotalIdle = totalIdle;

    return percent;
}

int UHD_SAFE_MAIN(int argc, char *argv[]){
    // example usage:
    // ./usrp_driver --antenna 1 --host usrp1 
    // spawn a usrp_server for each usrp
    
    uhd::set_thread_priority_safe(); 
   
    size_t rxshm_size, txshm_size;

    bool mimic_active;
    float mimic_delay;

    unsigned int iSide, iSwing; // often used loop variables

    int32_t verbose = 1; 
    int32_t rx_worker_status = 0; 
    int32_t mute_output = 0; // used if rx_worker error happends

    int32_t rx_stream_reset_count = 0;

    std::vector<sem_t>  sem_rx_vec(nSwings), sem_tx_vec(nSwings);

    std::vector<uint32_t> state_vec(nSwings, ST_INIT);
    uint32_t swing; // = SWING0;
    
    size_t nSamples_rx, nSamples_tx_pulse;


    uint32_t npulses, nerrors;
    ssize_t cmd_status;
    uint32_t usrp_driver_base_port, ip_part;

    int32_t connect_retrys = MAX_SOCKET_RETRYS; 
    int32_t sockopt;
    struct sockaddr_in sockaddr;
    struct sockaddr_storage client_addr;
    socklen_t addr_size;

    uhd::time_spec_t start_time, rx_start_time;
    
    // vector of all pulse start times over an integration period
    std::vector<uhd::time_spec_t> pulse_time_offsets;
    // vector of the sample index of pulse start times over an integration period
    std::vector<uint64_t> pulse_sample_idx_offsets;

    boost::thread_group uhd_threads;

    // process config file for port and SHM sizes
    DEBUG_PRINT("USRP_DRIVER starting to read driver_config.ini\n");
    boost::property_tree::ptree pt;
    boost::property_tree::ini_parser::read_ini("../driver_config.ini", pt);

  //  DEBUG_PRINT("USRP_DRIVER reading rxshm_size\n");
  //  std::cout << pt.get<std::string>("shm_settings.rxshm_size") << '\n';
    rxshm_size = std::stoi(pt.get<std::string>("shm_settings.rxshm_size"));
 
  //  DEBUG_PRINT("USRP_DRIVER reading txshm_size\n");
    txshm_size = std::stoi(pt.get<std::string>("shm_settings.txshm_size"));

    usrp_driver_base_port = std::stoi(pt.get<std::string>("network_settings.USRPDriverPort"));
    
    boost::property_tree::ptree pt_array;
    DEBUG_PRINT("USRP_DRIVER starting to read array_config.ini\n");
    boost::property_tree::ini_parser::read_ini("../array_config.ini", pt_array);
    mimic_active = std::stof(pt_array.get<std::string>("mimic.mimic_active")) != 0;
    mimic_delay  = std::stof(pt_array.get<std::string>("mimic.mimic_delay"));
    fprintf(stderr, "read from ini: mimic_active=%d, mimic_delay=%f\n", mimic_active, mimic_delay);

    // TODO also read usrp_config.ini and get antenna and side information from it. remove antenna input argument. 

    // process command line arguments
    struct arg_lit  *al_help   = arg_lit0(NULL, "help", "Prints help information and then exits");
//    struct arg_int  *ai_ant    = arg_intn("a", "antenna", NULL, 1, 2, "Antenna position index for the USRP"); 
    struct arg_int  *ai_ant_a  = arg_int0("a", "antennaA", NULL, "Antenna position index for the USRP on side A"); 
    struct arg_int  *ai_ant_b  = arg_int0("b", "antennaB", NULL, "Antenna position index for the USRP on side B"); 
    struct arg_str  *as_host   = arg_str0("h", "host", NULL, "Hostname or IP address of USRP to control (e.g usrp1)"); 
    struct arg_lit  *al_intclk = arg_lit0("i", "intclk", "Select internal clock (default is external)"); 
    struct arg_end  *ae_argend = arg_end(ARG_MAXERRORS);
    void* argtable[] = {al_help, ai_ant_a, ai_ant_b, as_host, al_intclk, ae_argend};
    
    double txrate, rxrate, txfreq, rxfreq;
    double txrate_new, rxrate_new, txfreq_new, rxfreq_new;

    DEBUG_PRINT("usrp_driver debug mode enabled\n");

    if (SUPRESS_UHD_PRINTS) {
        uhd::msg::register_handler(&uhd_term_message_handler);
    }

    nerrors = arg_parse(argc,argv,argtable);
    if (nerrors > 0) {
        arg_print_errors(stdout,ae_argend,"usrp_driver");
        exit(1);
    }
    if (argc == 1) {
        printf("No arguments found, try running again with --help for more information.\n");
        exit(1);
    }
    if(al_help->count > 0) {
        printf("Usage: ");
        arg_print_syntax(stdout,argtable,"\n");
        arg_print_glossary(stdout,argtable,"  %-25s %s\n");
        arg_freetable(argtable, sizeof(argtable)/sizeof(argtable[0]));
        return 0;
    }
   
    unsigned int nSides =  ai_ant_a->count + ai_ant_b->count; 
    if( nSides == 0 ) {
        printf("No antenna index, exiting...");
        return 0;
    }
    
    if(as_host->sval == NULL) {
        printf("Missing usrp host command line argument, exiting...");
        return 0;
    }

    std::vector<int> antennaVector(nSides);
    std::vector<uint64_t> channel_numbers;
    // both sides
    if( nSides == 2 ) {
        DEBUG_PRINT("Setting side A: ant_idx %d\n",ai_ant_a->ival[0]);
        antennaVector[0] = ai_ant_a->ival[0];
        channel_numbers.push_back(0);

        DEBUG_PRINT("Setting side B: ant_idx %d\n",ai_ant_b->ival[0]);
        antennaVector[1] = ai_ant_b->ival[0];
        channel_numbers.push_back(1);
    } else {
     // side A
     if (ai_ant_a->count == 1) {
        DEBUG_PRINT("Setting side A: ant_idx %d\n",ai_ant_a->ival[0]);
        antennaVector[0] = ai_ant_a->ival[0];
        channel_numbers.push_back(0);
     // side B
     } else {
        DEBUG_PRINT("Setting side B: ant_idx %d\n",ai_ant_b->ival[0]);
        antennaVector[0] = ai_ant_b->ival[0];
        channel_numbers.push_back(1);
        DEBUG_PRINT("Warning: For one side use DIO output is always on Side A!!!!!!!!!!!!!"); // TODO correct this


     }

    }

    // pointers to shared memory
    std::vector<std::vector<void *>> shm_rx_vec(nSides, std::vector<void *>( nSwings));
    std::vector<std::vector<void *>> shm_tx_vec(nSides, std::vector<void *>( nSwings));

    // local buffers for tx and rx
    std::vector<std::vector<std::complex<int16_t>>> tx_samples(nSides, std::vector<std::complex<int16_t>>(MAX_PULSE_LENGTH,0));
    std::vector<std::vector<std::complex<int16_t>>> rx_data_buffer(nSides, std::vector<std::complex<int16_t>>(0));
     

    std::string usrpargs(as_host->sval[0]);
//    usrpargs = "addr0=" + usrpargs + ",master_clock_rate=200.0e6";
    usrpargs = "addr0=" + usrpargs + ",master_clock_rate=200.0e6,recv_frame_size=8000";
    uhd::usrp::multi_usrp::sptr usrp = uhd::usrp::multi_usrp::make(usrpargs);
  //  usrp->set_rx_subdev_spec(uhd::usrp::subdev_spec_t("A:A B:A"));
  //  usrp->set_tx_subdev_spec(uhd::usrp::subdev_spec_t("A:A B:A"));
    boost::this_thread::sleep(boost::posix_time::seconds(SETUP_WAIT));
    uhd::stream_args_t stream_args("sc16", "sc16");
    
    if (usrp->get_rx_num_channels() < nSides || usrp->get_tx_num_channels() < nSides) {  
       DEBUG_PRINT("ERROR: Number of defined channels (%i) is smaller than avaialable channels:\n    usrp->get_rx_num_channels(): %lu \n    usrp->get_tx_num_channels(): %lu \n\n", nSides, usrp->get_rx_num_channels(),   usrp->get_tx_num_channels());
       return -1;
    }
    stream_args.channels = channel_numbers;
    uhd::rx_streamer::sptr rx_stream = usrp->get_rx_stream(stream_args);
    uhd::tx_streamer::sptr tx_stream = usrp->get_tx_stream(stream_args);


    // TODO: retry uhd connection if fails..

    // Determine port from 3rd part of ip (192.168.x.2 => port = base_port + x ) 
    int start_idx = usrpargs.find("."); 
    start_idx = usrpargs.find(".", start_idx+1);
    int end_idx = usrpargs.find(".", start_idx+1);
    ip_part = atoi(usrpargs.substr(start_idx+1, end_idx-start_idx-1).c_str());



    // initialize rxfe gpio
    kodiak_init_rxfe(usrp, nSides);
    // initialize other gpio on usrp
    init_timing_signals(usrp, mimic_active, nSides);
    
    //if(CAPTURE_ERRORS) {
    //    signal(SIGINT, siginthandler);
    //}
    
    // open shared memory buffers and semaphores created by cuda_driver.py
    // for dual polarization we use antenna numbers 20 to 35 (side is always 0)
    for(iSwing = 0; iSwing < nSwings; iSwing++) {
        for(iSide = 0; iSide < nSides; iSide++) {
            int shm_side = 0;
            shm_rx_vec[iSide][iSwing] = open_sample_shm(antennaVector[iSide], RXDIR, shm_side, iSwing, rxshm_size);
            shm_tx_vec[iSide][iSwing] = open_sample_shm(antennaVector[iSide], TXDIR, shm_side, iSwing, txshm_size);
            DEBUG_PRINT("usrp_driver rx shm addr: %p iSide: %d iSwing: %d\n", shm_rx_vec[iSide][iSwing], iSide, iSwing);

            if (antennaVector[iSide] < 19 ) { // semaphores only for antennas of first polarization TODO check if this is enough
               sem_rx_vec[iSwing] = open_sample_semaphore(antennaVector[iSide], iSwing, RXDIR);
               sem_tx_vec[iSwing] = open_sample_semaphore(antennaVector[iSide], iSwing, TXDIR);
            }
        }
    }


    if(al_intclk->count > 0) {
        usrp->set_clock_source("internal");
        usrp->set_time_source("internal");
    }
    else {
    // sync clock with external 10 MHz and PPS
        DEBUG_PRINT("Set clock: external\n");
        usrp->set_clock_source("external", 0);
        DEBUG_PRINT("Set time: external\n");
        usrp->set_time_source("external", 0);
        DEBUG_PRINT("Done setting time and clock\n");
     }

    while(true) {
        if(driversock) {
            close(driverconn);
            close(driversock);
        }
   
        boost::this_thread::sleep(boost::posix_time::seconds(SETUP_WAIT));

        // bind to socket for communication with usrp_server.py:
        driversock = socket(AF_INET, SOCK_STREAM, 0);
        if(driversock < 0){
            perror("opening stream socket\n");
            exit(1);
        }

        sockopt = 1;
        setsockopt(driversock, SOL_SOCKET, SO_REUSEADDR, &sockopt, sizeof(int32_t));

        sockaddr.sin_family = AF_INET;
        // TODO: maybe limit addr to interface connected to usrp_server
        sockaddr.sin_addr.s_addr = htonl(INADDR_ANY);

        fprintf(stderr, "listening on port: %d\n", usrp_driver_base_port + ip_part); 
        sockaddr.sin_port = htons(usrp_driver_base_port + ip_part);
        

        if( bind(driversock, (struct sockaddr *)&sockaddr, sizeof(sockaddr)) < 0){
                perror("binding tx stream socket");
                exit(1);
        }
   
        // wait for connection...
        listen(driversock, 5);

        // and accept it
        fprintf(stderr, "waiting for socket connection\n");
        addr_size = sizeof(client_addr);
        driverconn = accept(driversock, (struct sockaddr *) &client_addr, &addr_size);
        fprintf(stderr, "accepted socket connection\n");

        while(true) {
            // wait for transport endpoint to connect?
            
            DEBUG_PRINT("USRP_DRIVER waiting for command\n");
            uint8_t command = sock_get_cmd(driverconn, &cmd_status);
            DEBUG_PRINT("USRP_DRIVER received command, status: %zu\n", cmd_status);
             
            // see if socket is closed..
            if(cmd_status == 11 || cmd_status == 0 || cmd_status < 0) {
                DEBUG_PRINT("USRP_DRIVER lost connection to usrp_server, waiting for fresh connection, %d tries remaining\n", connect_retrys);
                close(driversock);
                if(connect_retrys-- < 0) {
                    exit(1);
                }
                sleep(1);
                break;
            }

          //  double cpuUsage = getCurrentCPUValue();
          //  DEBUG_PRINT("CPU usage: %f \n",cpuUsage );
 
            connect_retrys = MAX_SOCKET_RETRYS;
            switch(command) {
                case USRP_SETUP: {
                    // receive infomation about a pulse sequence/integration period
                    // transmit/receive center frequenies and sampling rates
                    // number of tx/rx samples
                    // number of pulse sequences per integration period, and pulse start times
                    swing      = sock_get_int16(  driverconn); 
                    
                    DEBUG_PRINT("entering USRP_SETUP command (swing %d)\n", swing);
                  
                    txfreq_new = sock_get_float64(driverconn);
                    rxfreq_new = sock_get_float64(driverconn);
                    txrate_new = sock_get_float64(driverconn);
                    rxrate_new = sock_get_float64(driverconn);

                    npulses = sock_get_uint32(driverconn);

                    nSamples_rx       = sock_get_uint64(driverconn);
                    nSamples_tx_pulse = sock_get_uint64(driverconn);

                    DEBUG_PRINT("USRP_SETUP number of requested rx samples: %d\n", (uint32_t) nSamples_rx);
                    DEBUG_PRINT("USRP_SETUP number of requested tx samples per pulse: %d\n", (uint32_t) nSamples_tx_pulse);
                    DEBUG_PRINT("USRP_SETUP existing tx rate : %f (swing %d)\n", txrate, swing);
                    DEBUG_PRINT("USRP_SETUP requested tx rate: %f\n", txrate_new);

                    // resize 
                    pulse_sample_idx_offsets.resize(npulses);
                    pulse_time_offsets.resize(npulses);

                    for(uint32_t i = 0; i < npulses; i++) {
                //        DEBUG_PRINT("USRP_SETUP waiting for pulse offset %d of %d\n", i+2, npulses);
                        pulse_sample_idx_offsets[i] = sock_get_uint64(driverconn); 
                //        DEBUG_PRINT("USRP_SETUP received %zu pulse offset\n", pulse_sample_idx_offsets[i]);

                    }

                    if(rx_data_buffer[0].size() != nSamples_rx) {
                       for(iSide = 0; iSide < nSides; iSide++) {
                           rx_data_buffer[iSide].resize(nSamples_rx);
                           
                       }
                    }
                /*    // DEGUG
                    for(iSide = 0; iSide < nSides; iSide++) {
                        DEBUG_PRINT("rx_data_buffer after init:\n  side %d", iSide);
                        for (int iSample=0; iSample<200; iSample++) {
                             DEBUG_PRINT("%lu, ", rx_data_buffer[iSide][iSample]);
                        }
                        DEBUG_PRINT("\n");
                    }
                 */

               /*     for (iSide=0; iSide<nSides;iSide++) {
                        DEBUG_PRINT("USRP_SETUP tx shm addr: %p \n", shm_tx_vec[iSide][swing]);
                    }
                 */   
                    // if necessary, retune USRP frequency and sampling rate
                    if(rxrate != rxrate_new) {
                        usrp->set_rx_rate(rxrate_new);
                        rxrate = usrp->get_rx_rate();
                    }

                    if(txrate != txrate_new) {
                        usrp->set_tx_rate(txrate_new);
                        txrate = usrp->get_tx_rate();
   
                    }
                    
                    if(rxfreq != rxfreq_new) {
                        usrp->set_rx_freq(rxfreq_new);
                        rxfreq = usrp->get_rx_freq();
                    }

                    if(txfreq != txfreq_new) {
                        usrp->set_tx_freq(txfreq_new);
                        txfreq = usrp->get_tx_freq();
                    }

                    if(verbose) {
                        std::cout << boost::format("Actual RX Freq: %f MHz...") % (usrp->get_rx_freq()/1e6)  <<  std::endl;
                        std::cout << boost::format("Actual RX Rate: %f Msps...") % (usrp->get_rx_rate()/1e6) <<  std::endl;
                        std::cout << boost::format("Actual TX Freq: %f MHz...") % (usrp->get_tx_freq()/1e6)  <<  std::endl;
                        std::cout << boost::format("Actual TX Rate: %f Msps...") % (usrp->get_tx_rate()/1e6) <<  std::endl;
                    }

                    // TODO: set the number of samples in a pulse. this is calculated from the pulse duration and the sampling rate 
                    // when do we know this? after USRP_SETUP
                
                    state_vec[swing] = ST_READY; 
                    DEBUG_PRINT("changing state_vec[swing] to ST_READY\n");
                    sock_send_uint8(driverconn, USRP_SETUP);
                    break;
                    }

                case RXFE_SET: {
                    DEBUG_PRINT("entering RXFE_SET command\n");
                    RXFESettings rf_settings;
                    rf_settings.amp1 = sock_get_uint8(driverconn);
                    rf_settings.amp2 = sock_get_uint8(driverconn);
                    uint8_t attTimes2 = sock_get_uint8(driverconn);
                    rf_settings.att_05_dB = ( attTimes2 & 0x01 ) != 0;
                    rf_settings.att_1_dB  = ( attTimes2 & 0x02 ) != 0;
                    rf_settings.att_2_dB  = ( attTimes2 & 0x04 ) != 0;
                    rf_settings.att_4_dB  = ( attTimes2 & 0x08 ) != 0;
                    rf_settings.att_8_dB  = ( attTimes2 & 0x10 ) != 0;
                    rf_settings.att_16_dB = ( attTimes2 & 0x20 ) != 0;
                   
                    kodiak_set_rxfe(usrp, rf_settings, nSides);
                    sock_send_uint8(driverconn, RXFE_SET);
                    break;
                    }

                case TRIGGER_PULSE: {
                    swing      = sock_get_int16(  driverconn); 
                    DEBUG_PRINT("entering TRIGGER_PULSE command (swing %d)\n", swing );

                    if (state_vec[swing] != ST_READY) {
                        sock_send_uint8(driverconn, TRIGGER_BUSY);
                        DEBUG_PRINT("TRIGGER_PULSE busy in state_vec[swing] %d, returning\n", state_vec[swing]);
                    }
                    else {
                        std::complex<int16_t> *shm_pulseaddr;
                        DEBUG_PRINT("TRIGGER_PULSE ready\n");
                        state_vec[swing] = ST_PULSE;

                        DEBUG_PRINT("TRIGGER_PULSE locking semaphore\n");
                        lock_semaphore(sem_rx_vec[swing]); 
                        lock_semaphore(sem_tx_vec[swing]); 

                        DEBUG_PRINT("TRIGGER_PULSE semaphore locked\n");

                        // create local copy of transmit pulse data from shared memory
                        size_t spb = tx_stream->get_max_num_samps();
                        size_t pulse_bytes = sizeof(std::complex<int16_t>) * nSamples_tx_pulse;


                        // TODO side loop
                        for (iSide = 0; iSide<nSides; iSide++) {
                            tx_samples[iSide].resize(nSamples_tx_pulse+2*spb);                    
                            shm_pulseaddr = &((std::complex<int16_t> *) shm_tx_vec[iSide][swing])[0];
                            memcpy(&tx_samples[iSide][spb], shm_pulseaddr, pulse_bytes);
                        }

                        if(SAVE_RAW_SAMPLES_DEBUG) {
                            FILE *raw_dump_fp;
                            char raw_dump_name[80];
                            int nExportSamples =  nSamples_tx_pulse+2*spb;
                           // DEBUG_PRINT("Exporting %i raw tx_samples (%i + 2* %i)\n", nExportSamples, nSamples_tx_pulse, spb);
                            for (iSide =0; iSide < nSides; iSide++){
                                sprintf(raw_dump_name,"diag/raw_samples_tx_ant_%d.cint16", antennaVector[iSide]);
                                raw_dump_fp = fopen(raw_dump_name, "wb");
                                fwrite(&tx_samples[iSide][0], sizeof(std::complex<int16_t>),nExportSamples , raw_dump_fp);
                                fclose(raw_dump_fp);
                            }
                        }

                        // read in time for start of pulse sequence over socket
                        uint32_t pulse_time_full = sock_get_uint32(driverconn);
                        double pulse_time_frac = sock_get_float64(driverconn);
                        start_time = uhd::time_spec_t(pulse_time_full, pulse_time_frac);
                        double tr_to_pulse_delay = sock_get_float64(driverconn);


                        
                        // calculate usrp clock time of the start of each pulse over the integration period
                        // so we can schedule the io (perhaps we will have to move io off of the usrp if it can't keep up)
                        for(uint32_t p_i = 0; p_i < pulse_time_offsets.size(); p_i++) {
                            double offset_time = pulse_sample_idx_offsets[p_i] / txrate;
                            pulse_time_offsets[p_i] = offset_time_spec(start_time, offset_time);
                            DEBUG_PRINT("TRIGGER_PULSE pulse time %d is %2.5f\n", p_i, pulse_time_offsets[p_i].get_real_secs());
                        }

                        DEBUG_PRINT("first TRIGGER_PULSE time is %2.5f and last is %2.5f\n", pulse_time_offsets[0].get_real_secs(), pulse_time_offsets.back().get_real_secs());

                        rx_start_time = offset_time_spec(start_time, tr_to_pulse_delay/1e6);
                        rx_start_time = offset_time_spec(rx_start_time, pulse_sample_idx_offsets[0]/txrate); 

       
                        // send_timing_for_sequence(usrp, start_time, pulse_times);
                        double pulseLength = nSamples_tx_pulse / txrate;
                        
                        // float debugt = usrp->get_time_now().get_real_secs();
                        // DEBUG_PRINT("USRP_DRIVER: spawning worker threads at usrp_time %2.4f\n", debugt);

                        DEBUG_PRINT("TRIGGER_PULSE creating rx and tx worker threads on swing %d (nSamples_rx= %d\n", swing,(int) nSamples_rx);
                        // works fine with tx_worker and dio_worker, fails if rx_worker is enabled
                        uhd_threads.create_thread(boost::bind(usrp_rx_worker, usrp, rx_stream, &rx_data_buffer, nSamples_rx, rx_start_time, &rx_worker_status)); 
                        uhd_threads.create_thread(boost::bind(usrp_tx_worker, tx_stream, tx_samples, start_time, pulse_sample_idx_offsets)); 
                        uhd_threads.create_thread(boost::bind(send_timing_for_sequence, usrp, start_time,  pulse_time_offsets, pulseLength, mimic_active, mimic_delay, nSides)); 

                        uhd_threads.join_all(); // wait for transmit threads to finish, drawn from shared memory..
                        DEBUG_PRINT("TRIGGER_PULSE rx_worker, tx_worker and dio threads on swing %d\n joined.", swing);

                        sock_send_uint8(driverconn, TRIGGER_PULSE);

                    }


                    break;
                    }

                case READY_DATA: {
                    swing      = sock_get_int16(  driverconn); 
                    DEBUG_PRINT("READY_DATA command (swing %d), waiting for uhd threads to join back\n", swing);

                    
                    DEBUG_PRINT("READY_DATA unlocking swing a semaphore\n");
                    unlock_semaphore(sem_rx_vec[swing]);
                    unlock_semaphore(sem_tx_vec[swing]);
        
                    DEBUG_PRINT("READY_DATA usrp worker threads joined, semaphore unlocked, sending metadata\n");
                    // TODO: handle multiple channels of data.., use channel index to pick correct swath of memory to copy into shm
                  
                   // TODO: delete this
                   // uint32_t channel_index;
                   // channel_index = sock_get_int32(driverconn);
                    if(rx_worker_status){
                      fprintf(stderr, "Error in rx_worker. Setting state to %d.\n", rx_worker_status);
                      state_vec[swing] = rx_worker_status;
                      rx_worker_status = 0;
                      mute_output = 1;                  
                       
                        
                      if(rx_worker_status != RX_WORKER_STREAM_TIME_ERROR) {
                          // recreate rx_stream unless the error was from sending the stream command too late
                          rx_stream_reset_count++;
                          fprintf(stderr, "READY_DATA: recreating rx_stream (%dth time)!\n", rx_stream_reset_count);
                          rx_stream.reset();
                          rx_stream = usrp->get_rx_stream(stream_args);
                      }
                    }
    
                    DEBUG_PRINT("READY_DATA state: %d, ant: %d, num_samples: %zu\n", state_vec[swing], antennaVector[0], nSamples_rx);
                    sock_send_int32(driverconn, state_vec[swing]);  // send status
                    sock_send_int32(driverconn, antennaVector[0]);   // send antenna TODO do this for both antennas?
                    sock_send_int32(driverconn, nSamples_rx);     // nsamples;  send send number of samples
                   
                    // read FAULT status   
                    bool fault;
                    for (iSide =0; iSide<nSides; iSide++){  
                        fault = read_FAULT_status_from_control_board(usrp, iSide);
                    }
                    // TODO move this in loop as soon as usrp_server receives both sides
                    sock_send_bool(driverconn, fault);     // FAULT status from conrol board
                
                /*    // DEGUG
                    for(iSide = 0; iSide < nSides; iSide++) {
                        DEBUG_PRINT("rx_data_buffer before copy:\n  side %d", iSide);
                        for (int iSample=0; iSample<200; iSample++) {
                             DEBUG_PRINT("%d, ", rx_data_buffer[iSide][iSample]);
                        }
                        DEBUG_PRINT("\n");
                    }
                  */
  
                    if (mute_output) {
                       DEBUG_PRINT("READY_DATA: Filling SHM with zeros (because of rx_worker error) \n");
                       for (iSide = 0; iSide<nSides; iSide++) {
                      //    std::fill(shm_rx_vec[iSide][swing][0], shm_rx_vec[iSide][swing][rxshm_size/4], 0);
                          memset(shm_rx_vec[iSide][swing], 0, rxshm_size);
                       }
                       mute_output = 0;
                    }
                    else {
                        DEBUG_PRINT("READY_DATA starting copying rx data buffer to shared memory\n");
                        for (iSide = 0; iSide<nSides; iSide++) {
                            // DEBUG_PRINT("usrp_drivercopy to rx shm addr: %p iSide: %d iSwing: %d\n", shm_rx_vec[iSide][swing], iSide, iSwing);
                            memcpy(shm_rx_vec[iSide][swing], &rx_data_buffer[iSide][0], sizeof(std::complex<int16_t>) * nSamples_rx);
                        }
                    }

                    if(SAVE_RAW_SAMPLES_DEBUG) {
                        FILE *raw_dump_fp;
                        char raw_dump_name[80];
                        for (iSide=0; iSide<nSides; iSide++) {
                           sprintf(raw_dump_name,"diag/raw_samples_rx_ant_%d.cint16", antennaVector[iSide]);
                           raw_dump_fp = fopen(raw_dump_name, "wb");
                           fwrite(&rx_data_buffer[iSide], sizeof(std::complex<int16_t>), nSamples_rx, raw_dump_fp);
                           fclose(raw_dump_fp);
                        }

                    }

                    DEBUG_PRINT("READY_DATA finished copying rx data buffer to shared memory\n");
                    state_vec[swing] = ST_READY; 
                    DEBUG_PRINT("changing state_vec[swing] to ST_READY\n");

                    DEBUG_PRINT("READY_DATA returning command success \n");
                    sock_send_uint8(driverconn, READY_DATA);
                    break;
                    }

                case UHD_GETTIME: {
                    DEBUG_PRINT("entering UHD_GETTIME command\n");
                    start_time = usrp->get_time_now();

                    uint32_t real_time = start_time.get_real_secs();
                    double frac_time = start_time.get_frac_secs();

                    sock_send_uint32(driverconn, real_time);
                    sock_send_float64(driverconn, frac_time);

                    DEBUG_PRINT("UHD_GETTIME current UHD time: %d %.2f command\n", real_time, frac_time);
                    sock_send_uint8(driverconn, UHD_GETTIME);
                    break;
                    }
                // command to reset time, sync time with external PPS pulse
                case UHD_SYNC: {
                    DEBUG_PRINT("entering UHD_SYNC command\n");
                    // if --intclk flag passed to usrp_driver, set clock source as internal and do not sync time
                    if(al_intclk->count > 0) {
                        usrp->set_time_now(uhd::time_spec_t(0.0));
                    }

                    else {

                 /*       const uhd::time_spec_t last_pps_time = usrp->get_time_last_pps();
                        while (last_pps_time == usrp->get_time_last_pps()) {
                            boost::this_thread::sleep(boost::posix_time::milliseconds(100));
                        }
                        usrp->set_time_next_pps(uhd::time_spec_t(0.0));
                        boost::this_thread::sleep(boost::posix_time::milliseconds(1100));
                 */
                        DEBUG_PRINT("Start setting unknown pps\n");
                        usrp->set_time_unknown_pps(uhd::time_spec_t(11.0));
                        DEBUG_PRINT("end setting unknown pps\n");
                     }

                    sock_send_uint8(driverconn, UHD_SYNC);
                    break;
                    }
                case CLRFREQ: {
                    DEBUG_PRINT("entering CLRFREQ command\n");
                    uhd::rx_metadata_t md;
                    double timeout = 5.0; // TODO: set this dynamically using max delay for clrfreq search

                    uint32_t num_clrfreq_samples = sock_get_uint32(driverconn);
                    uint32_t clrfreq_time_full   = sock_get_uint32(driverconn);
                    double clrfreq_time_frac     = sock_get_float64(driverconn);
                    double clrfreq_cfreq         = sock_get_float64(driverconn);
                    double clrfreq_rate          = sock_get_float64(driverconn);

                    uint32_t real_time; 
                    double frac_time;

                    size_t num_acc_samps = 0;
                    const size_t num_max_request_samps = rx_stream->get_max_num_samps();

                    DEBUG_PRINT("CLRFREQ time: %d . %.2f \n", clrfreq_time_full, clrfreq_time_frac);
                    DEBUG_PRINT("CLRFREQ rate: %.2f, CLRFREQ_nsamples %d, freq: %.2f\n", clrfreq_rate, num_clrfreq_samples, clrfreq_cfreq);
                    uhd::time_spec_t clrfreq_start_time = uhd::time_spec_t(clrfreq_time_full, clrfreq_time_frac);
                    real_time = clrfreq_start_time.get_real_secs();
                    frac_time = clrfreq_start_time.get_frac_secs();
                    DEBUG_PRINT("CLRFREQ UHD clrfreq target time: %d %.2f \n", real_time, frac_time);

                    std::vector<std::complex<int16_t>> clrfreq_data_buffer;
                    clrfreq_data_buffer.resize(num_clrfreq_samples);

                    // TODO: does this take too long?
                    usrp->set_rx_rate(clrfreq_rate);
                    usrp->set_rx_freq(clrfreq_cfreq);
                    
                    // verify that rate is set..
                    clrfreq_rate = usrp->get_rx_rate(); // read back actual rate
                    DEBUG_PRINT("CLRFREQ actual rate: %.2f\n", clrfreq_rate);

                    // set up for USRP sampling
                    md.error_code = uhd::rx_metadata_t::ERROR_CODE_NONE;
                    uhd::stream_cmd_t stream_cmd = uhd::stream_cmd_t::STREAM_MODE_NUM_SAMPS_AND_DONE;
                    stream_cmd.num_samps = num_clrfreq_samples;
                    stream_cmd.stream_now = false;
                    stream_cmd.time_spec = clrfreq_start_time;
 
                    start_time = usrp->get_time_now();
                    real_time = start_time.get_real_secs();
                    frac_time = start_time.get_frac_secs();
                    DEBUG_PRINT("CLRFREQ UHD before waiting for samples : %d %.2f \n", real_time, frac_time);

                    usrp->issue_stream_cmd(stream_cmd);                   
                    DEBUG_PRINT("CLRFREQ starting to grab samples\n");
                    // and start grabbin'
                    // so, we're segfaulting on rx_stream->recv, check data buffers
                    while(num_acc_samps < num_clrfreq_samples) {
                        size_t samp_request = std::min(num_max_request_samps, num_clrfreq_samples - num_acc_samps);
                        DEBUG_PRINT("CLRFREQ requesting %zu samples with timeout %.2f\n", samp_request, timeout);
                        size_t num_rx_samps = rx_stream->recv(&((clrfreq_data_buffer)[num_acc_samps]), samp_request, md, timeout);

                        timeout = .1; 

                        //handle the error codes
                        if (md.error_code == uhd::rx_metadata_t::ERROR_CODE_TIMEOUT) break;
                        if (md.error_code != uhd::rx_metadata_t::ERROR_CODE_NONE){
                            throw std::runtime_error(str(boost::format(
                                "Receiver error %s"
                            ) % md.strerror()));
                        }

                        if (DEBUG) {
                            start_time = usrp->get_time_now();
                            real_time = start_time.get_real_secs();
                            frac_time = start_time.get_frac_secs();
                            std::cout << boost::format("Received packet: %u samples") % num_rx_samps << std::endl;
                            DEBUG_PRINT("CLRFREQ UHD time: %d %.2f \n", real_time, frac_time);
                        }
                        num_acc_samps += num_rx_samps;
                    }

                    if (md.error_code == uhd::rx_metadata_t::ERROR_CODE_TIMEOUT) {
                    uhd::time_spec_t rx_error_time = usrp->get_time_now();
                        std::cerr << "Timeout encountered at " << rx_error_time.get_real_secs() << std::endl;
                    }
                    if (md.error_code == uhd::rx_metadata_t::ERROR_CODE_OVERFLOW){
                    uhd::time_spec_t rx_error_time = usrp->get_time_now();
                        std::cerr << "Overflow encountered at " << rx_error_time.get_real_secs() << std::endl;
                    }
                    if (md.error_code != uhd::rx_metadata_t::ERROR_CODE_NONE){
                    uhd::time_spec_t rx_error_time = usrp->get_time_now();
                        std::cerr << "Unexpected error code " << md.error_code <<
                    " encountered at " << rx_error_time.get_real_secs() << std::endl;
                    }
                    DEBUG_PRINT("CLRFREQ received samples, relaying them back...\n");
                    
                    sock_send_int32(driverconn, (int32_t) antennaVector[0]); // TODO both sides?
                    sock_send_float64(driverconn, clrfreq_rate);

                    // send back samples                   
                    for(uint32_t i = 0; i < num_clrfreq_samples; i++) {
                        sock_send_cshort(driverconn, clrfreq_data_buffer[i]);
                    }

                    // restore usrp rates
                    usrp->set_rx_rate(rxrate);
                    usrp->set_rx_freq(rxfreq);

                    sock_send_uint8(driverconn, CLRFREQ);
                    start_time = usrp->get_time_now();
                    real_time = start_time.get_real_secs();
                    frac_time = start_time.get_frac_secs();
                    DEBUG_PRINT("CLRFREQ finished at UHD time: %d %.2f \n", real_time, frac_time);

                    break;

                    }

                case EXIT: {
                    DEBUG_PRINT("entering EXIT command\n");
                    close(driverconn);

                    for(iSide = 0; iSide < nSides; iSide++) {
                        for(iSwing = 0; iSwing < nSwings; iSwing++) {
                            munmap(shm_rx_vec[iSide][iSwing], rxshm_size);
                            munmap(shm_tx_vec[iSide][iSwing], txshm_size);
                            sem_close(&sem_rx_vec[iSwing]);
                            sem_close(&sem_tx_vec[iSwing]);
                        }
                    }

                    // TODO: close usrp streams?
//                    sock_send_uint8(driverconn, EXIT);
                    exit(1);
                    
                    break;
                    }

                default: {
                    printf("USRP_DRIVER unrecognized command: %d, %c, exiting..\n", command, command);
                    sleep(10);
                    exit(1);
                    break;
                }
            }
        }
    }
    
    return 0;
}

