#!/usr/bin/env python

"""bigjob_agent: bigjob agent that is executed on the resource
"""


import sys
import os
import bigjob.state
import socket
import threading
import time
import pdb
import traceback
import ConfigParser
import types
import logging
logging.basicConfig(level=logging.DEBUG)

try:
    import saga
except:
    logging.warning("SAGA could not be found. Not all functionalities working")

sys.path.append(os.path.dirname(os.path.abspath(__file__)) + "/../../ext/threadpool-1.2.7/src/")
logging.debug(str(sys.path))
from threadpool import *
from bigjob import logger

if sys.version_info < (2, 5):
    sys.path.append(os.path.dirname( __file__ ) + "/../../ext/uuid-1.30/")
    sys.stderr.write("Warning: Using unsupported Python version\n")
if sys.version_info < (2, 4):
    sys.path.append(os.path.dirname( __file__ ) + "/../../ext/subprocess-2.6.4/")
    sys.stderr.write("Warning: Using unsupported Python version\n")
if sys.version_info < (2, 3):
    sys.stderr.write("Warning: Python versions <2.3 not supported\n")
    sys.exit(-1)

import subprocess

""" Config parameters (will move to config file in future) """
CONFIG_FILE="bigjob_agent.conf"
THREAD_POOL_SIZE=4
APPLICATION_NAME="bigjob"

class bigjob_agent:
    
    """BigJob Agent:
       - reads new job information from communication and coordination subsystem (Redis)
       - starts new jobs
       - monitors running jobs """
   
    """Constructor"""
    def __init__(self, args):
        
        self.coordination_url = args[1]
        # objects to store running jobs and processes
        self.jobs = []
        self.processes = {}
        self.freenodes = []
        self.busynodes = []
        self.restarted = {}

        # read config file
        # conf_file = os.path.dirname(args[0]) + "/" + CONFIG_FILE
        # conf_file = os.path.dirname(os.path.abspath( __file__ )) + "/" + CONFIG_FILE
        conf_file = os.path.dirname(os.path.abspath( __file__ )) + "/../" + CONFIG_FILE
        config = ConfigParser.ConfigParser()
        logging.debug ("read configfile: " + conf_file)
        config.read(conf_file)
        default_dict = config.defaults()        
        self.CPR = default_dict["cpr"]
        self.SHELL=default_dict["shell"]
        self.MPIRUN=default_dict["mpirun"]
        self.LAUNCH_METHOD=self.__get_launch_method(default_dict["launch_method"])
        
        logging.debug("Launch Method: " + self.LAUNCH_METHOD + " mpi: " + self.MPIRUN + " shell: " + self.SHELL)
        
        # init rms (SGE/PBS)
        self.init_rms()
        self.failed_polls = 0
        
        ##############################################################################
        # initialization of coordination and communication subsystem
        # Redis initialization
        self.base_url = args[2]
        self.id = self.__get_bj_id(self.base_url)
        logger.debug("BigJob Agent arguments: " + str(args))
        logger.debug("Initialize C&C subsystem to pilot-url: " + self.base_url)
        logger.debug("BigJob ID: %s"%self.id)
        
        # create bj directory
        self.bj_dir = os.path.join(os.getcwd(), self.id)
        try:
            os.makedirs(self.bj_dir)
        except:
            logger.debug("Directory already exists.")
        
        os.chdir(self.bj_dir)
        
        if(self.coordination_url.startswith("advert://") or self.coordination_url.startswith("sqlasyncadvert://")):
            try:
                from coordination.bigjob_coordination_advert import bigjob_coordination
                logging.debug("Utilizing ADVERT Backend: " + self.coordination_url)
            except:
                logger.error("Advert Backend could not be loaded")
                exc_type, exc_value, exc_traceback = sys.exc_info()
                traceback.print_exc(file=sys.stderr)
                traceback.print_tb(exc_traceback, file=sys.stderr)
        elif (self.coordination_url.startswith("redis://")):
            try:
                from coordination.bigjob_coordination_redis import bigjob_coordination      
                logger.debug("Utilizing Redis Backend: " + self.coordination_url + ". Please make sure Redis server is configured in bigjob_coordination_redis.py")
            except:
                logger.error("Error loading pyredis.")
        elif (self.coordination_url.startswith("tcp://")):
            try:
                from coordination.bigjob_coordination_zmq import bigjob_coordination
                logger.debug("Utilizing ZMQ Backend")
            except:
                logger.error("ZMQ Backend not found. Please install ZeroMQ (http://www.zeromq.org/intro:get-the-software) and " 
                      +"PYZMQ (http://zeromq.github.com/pyzmq/)")

        self.coordination = bigjob_coordination(server_connect_url=self.coordination_url)
    
        # update state of pilot job to running
        self.coordination.set_pilot_state(self.base_url, str(bigjob.state.Running), False)

        
        ##############################################################################
        # start background thread for polling new jobs and monitoring current jobs
        self.resource_lock=threading.RLock()
        self.threadpool = ThreadPool(THREAD_POOL_SIZE)
        
        self.launcher_thread=threading.Thread(target=self.dequeue_new_jobs)
        self.launcher_thread.start()
        
        self.monitoring_thread=threading.Thread(target=self.start_background_thread)
        self.monitoring_thread.start()
        
    
    def __get_bj_id(self, url):
        logger.debug("parsing ID out of URL: %s"%url)
        start = url.index("bj-")
        end = url.index(":", start)
        bj_id = url[start:end]
        return bj_id
    
    
    def init_rms(self):
        if(os.environ.get("PBS_NODEFILE")!=None):
            return self.init_pbs()
        elif(os.environ.get("PE_HOSTFILE")!=None):
            return self.init_sge()
        else:
            return self.init_local()
        return None

    def init_local(self):
        """ initialize free nodes list with dummy (for fork jobs)"""
        try:
            num_cpus = self.get_num_cpus()
            for i in range(0, num_cpus): 
                self.freenodes.append("localhost\n")
        except IOError:
            self.freenodes=["localhost\n"]

    def init_sge(self):
        """ initialize free nodes list from SGE environment """
        sge_node_file = os.environ.get("PE_HOSTFILE")    
        if sge_node_file == None:
                return
        f = open(sge_node_file)
        sgenodes = f.readlines()
        f.close()
        for i in sgenodes:    
        
            columns = i.split()                
            try:
                for j in range(0, int(columns[1])):
                    logger.debug("add host: " + columns[0].strip())
                    self.freenodes.append(columns[0]+"\n")
            except:
                    pass
        return self.freenodes            

    def init_pbs(self):
        """ initialize free nodes list from PBS environment """
        if self.LAUNCH_METHOD == "aprun":
            # Workaround for Kraken
            # PBS_NODEFILE does only contain front node
            # thus we create a dummy node file with the respective 
            # number of slots
            # aprun does not rely on the nodefile for job launching
            number_nodes =  os.environ.get("PBS_NNODES")
            self.freenodes=[]
            for i in range(0, int(number_nodes)):
                slot = "slot-%d\n"%i
                logger.debug("add slot: " + slot)
                self.freenodes.append(slot)            
        else:
            pbs_node_file = os.environ.get("PBS_NODEFILE")    
            if pbs_node_file == None:
                return
            f = open(pbs_node_file)
            self.freenodes = f.readlines()
            f.close()
    
            # check whether pbs node file contains the correct number of nodes
            num_cpus = self.get_num_cpus()
            node_dict={}
            for i in set(self.freenodes):
                node_dict[i] = self.freenodes.count(i)
                if node_dict[i] < num_cpus:
                    node_dict[i] = num_cpus
        
            self.freenodes=[]
            for i in node_dict.keys():
                logger.debug("host: " + i + " nodes: " + str(node_dict[i]))
                for j in range(0, node_dict[i]):
                    logger.debug("add host: " + i.strip())
                    self.freenodes.append(i)

    def get_num_cpus(self):
        cpuinfo = open("/proc/cpuinfo", "r")
        cpus = cpuinfo.readlines()
        cpuinfo.close()
        num = 0
        for i in cpus:
                if i.startswith("processor"):
                        num = num+1
        return num
        
     
    def execute_job(self, job_url, job_dict):
        """ obtain job attributes from c&c and execute process """
        state=str(job_dict["state"])
       
        #try:
        #   state = self.coordination.get_job_state(job_url)
        #except:
        #    logger.error("Could not access job state... skip execution attempt and requeuing job")
        #    result = self.coordination.queue_job(self.base_url, job_url)
        #    if result == False:
        #        self.coordination.set_job_state(job_url, str(saga.job.Failed))
        
        if(state==str(bigjob.state.Unknown) or
            state==str(bigjob.state.New)):
            try:
                #job_dict["state"]=str(saga.job.New)  
                job_id = job_dict["job-id"]              
                logger.debug("Start job id %s specification %s: "%(job_id, str(job_dict)))        
                numberofprocesses = "1"
                try:
                    if (job_dict.has_key("NumberOfProcesses") == True):
                        numberofprocesses = job_dict["NumberOfProcesses"]
                except:
                    pass # ignore in particular if Bliss is used
                
                spmdvariation="single"
                try:
                    if (job_dict.has_key("SPMDVariation") == True):
                        spmdvariation = job_dict["SPMDVariation"]
                except:
                    pass  # ignore in particular if Bliss is used
                
                arguments = ""
                if (job_dict.has_key("Arguments") == True):
                    arguments_raw = job_dict['Arguments'];
                    if type(arguments_raw) == types.ListType:
                        arguments_list = arguments_raw
                    else:
                        arguments_list = eval(job_dict["Arguments"])                    
                    for i in arguments_list:
                        arguments = arguments + " " + i
                        
                environment = os.environ
                envi = ""
                self.number_subjobs=1
                if (job_dict.has_key("Environment") == True):
                    env_raw = job_dict['Environment']
                    if type(env_raw) == types.ListType:
                        env_list = env_raw
                    else:
                        env_list = eval(job_dict["Environment"])

                    logger.debug("Environment: " + str(env_list))
                    for i in env_list:
                        logger.debug("Eval " + i)
                        if i.startswith("NUMBER_SUBJOBS"):
                            self.number_subjobs=int(i.split("=")[1].strip())
                            logger.debug("NUMBER_SUBJOBS: " + str(self.number_subjobs))
                        else:
                            envi_1 = "export " + i +"; "
                            envi = envi + envi_1
                            logger.debug(envi) 
                
                executable = job_dict["Executable"]
                
                workingdirectory = os.path.join(os.getcwd(), job_id)  
                if (job_dict.has_key("WorkingDirectory") == True):
                        workingdirectory =  job_dict["WorkingDirectory"]
                try:
                    os.makedirs(workingdirectory)
                except:
                    logger.debug("Directory %s already exists."%workingdirectory)
                logging.debug("Sub-Job: %s, Working_directory: %s"%(job_id, workingdirectory))
                
                
                output="stdout"
                if (job_dict.has_key("Output") == True):
                    output = job_dict["Output"]
                if not os.path.isabs(output):
                    output=os.path.join(workingdirectory, output)
                    
                error=os.path.join(workingdirectory,"stderr")
                if (job_dict.has_key("Error") == True):
                    error = job_dict["Error"]
                if not os.path.isabs(error):
                    error=os.path.join(workingdirectory, error)
                
                # append job to job list
                self.jobs.append(job_url)
                
                # create stdout/stderr file descriptors
                output_file = os.path.abspath(output)
                error_file = os.path.abspath(error)
                logger.debug("stdout: " + output_file + " stderr: " + error_file)
                stdout = open(output_file, "w")
                stderr = open(error_file, "w")
                if self.LAUNCH_METHOD=="aprun":
                    env_strip = envi.strip()
                    env_command = env_strip[:(len(env_strip)-1)]
                    command = "aprun  -n " + str(self.number_subjobs) + " -d " + numberofprocesses + " " + executable + " " + arguments

                    # MPMD Mode => all subjobs on Kraken fail because aprun returns 1 as returncode
                    #command = "aprun"
                    #for i in range(0, self.number_subjobs):
                    #    command = command +   " -d " + numberofprocesses + " " + executable + " " + arguments  
                    #    # + " 1 > "+ str(i)+ "-out.txt " + " 2 > "+ str(i)+ "-err.txt"
                    #    if i != self.number_subjobs-1:
                    #        command = command + " : "
                elif (spmdvariation.lower()!="mpi"):
                    command =  envi + executable + " " + arguments
                else:
                    command =  executable + " " + arguments
                #pdb.set_trace()
                # special setup for MPI NAMD jobs
                machinefile = self.allocate_nodes(job_dict)
                host = "localhost"
                try:
                    machine_file_handler = open(machinefile, "r")
                    node= machine_file_handler.readlines()
                    machine_file_handler.close()
                    host = node[0].strip()
                except:
                    pass


                if(machinefile==None):
                    logger.debug("Not enough resources to run: " + job_url)
                    self.coordination.queue_job(self.base_url, job_url)
                    return # job cannot be run at the moment
                
                # build execution command
                if self.LAUNCH_METHOD == "aprun":
                    command ="cd " + workingdirectory + "; " + command
                else:
                    if (spmdvariation.lower( )=="mpi"):
                        command = "cd " + workingdirectory + "; " + envi +  self.MPIRUN + " -np " + numberofprocesses + " -machinefile " + machinefile + " " + command
                    elif host == "localhost":
                        command ="cd " + workingdirectory + "; " + command
                    else:    
                        command ="ssh  " + host + " \"cd " + workingdirectory + "; " + command +"\""
                        
                
                # start application process                    
                shell = self.SHELL 
                logger.debug("execute: " + command + " in " + workingdirectory + " from: " + str(socket.gethostname()) + " (Shell: " + shell +")")
                # bash works fine for launching on QB but fails for Abe :-(
                p = subprocess.Popen(args=command, executable=shell, stderr=stderr,
                                     stdout=stdout, cwd=workingdirectory, 
                                     env=environment, shell=True)
                logger.debug("started " + command)
                self.processes[job_url] = p
                self.coordination.set_job_state(job_url, str(bigjob.state.Running))
            except:
                traceback.print_exc(file=sys.stderr)
    
   
            
    def allocate_nodes(self, job_dict):
        """ allocate nodes
            allocated nodes will be written to machinefile advert-launcher-machines-<jobid>
        """
        self.resource_lock.acquire()
        number_nodes = int(job_dict["NumberOfProcesses"])
        nodes = []
        machine_file_name = None
        if (len(self.freenodes)>=number_nodes):
            unique_nodes=set(self.freenodes)
            for i in unique_nodes:
                number = self.freenodes.count(i)
                logger.debug("allocate: " + i + " number nodes: " + str(number) 
                              + " current busy nodes: " + str(self.busynodes) 
                              + " free nodes: " + str(self.freenodes))
                for j in range(0, number):
                    if(number_nodes > 0):
                        nodes.append(i)
                        self.freenodes.remove(i)                
                        self.busynodes.append(i)
                        number_nodes = number_nodes - 1
                    else:
                        break

            machine_file_name = self.get_machine_file_name(job_dict)
            machine_file = open(machine_file_name, "w")
            #machine_file.writelines(self.freenodes[:number_nodes])
            machine_file.writelines(nodes)
            machine_file.close() 
            logger.debug("wrote machinefile: " + machine_file_name + " Nodes: " + str(nodes))
            # update node structures
            #self.busynodes.extend(self.freenodes[:number_nodes])
            #del(self.freenodes[:number_nodes])            
        
        self.resource_lock.release()
        return machine_file_name
        
    
    
    def setup_charmpp_nodefile(self, allocated_nodes):
        """ Setup charm++ nodefile to use for executing NAMD  
            HACK!! Method violates layering principle
            File $HOME/machinefile in charm++ nodefileformat is written to first node in list
        """
        # Nodelist format:
        # 
        # host tp-x001 ++cpus 2 ++shell ssh 
        # host tp-x002 ++cpus 2 ++shell ssh
        
        nodefile_string=""
        for i in allocated_nodes:
            if i.has_key("private_hostname"):
                nodefile_string=nodefile_string + "host "+ i["private_hostname"] + " ++cpus " + str(i["cpu_count"]) + " ++shell ssh\n"
            else:
                nodefile_string=nodefile_string + "host "+ i["hostname"] + " ++cpus " + str(i["cpu_count"]) + " ++shell ssh\n"
            
        # copy nodefile to rank 0 node
        jd = saga.job.description()
        jd.executable = "echo"
        jd.number_of_processes = "1"
        jd.spmd_variation = "single"
        # ssh root@tp-x001.ci.uchicago.edu "cat ~/.ssh/id_rsa.pub >> ~/.ssh/authorized_keys"
        jd.arguments = ["\""+nodefile_string+"\"", ">", "machinefile"]
        jd.output = "stdout.txt"
        jd.error = "stderr.txt"
        
        job_service_url = saga.url("ssh://root@"+allocated_nodes[0]["hostname"])
        job_service = saga.job.service(self.session, job_service_url)
        job = job_service.create_job(jd)
        job.run()
        job.wait()
    
    
    def print_machine_file(self, filename):
        fh = open(filename, "r")
        lines = fh.readlines()
        fh.close
        logger.debug("Machinefile: " + filename + " Hosts: " + str(lines))
       
         
    def free_nodes(self, job_url):
        job_dict = self.coordination.get_job(job_url)
        self.resource_lock.acquire()
        number_nodes = int(job_dict["NumberOfProcesses"])
        machine_file_name = self.get_machine_file_name(job_dict)
        logger.debug("Machine file: " + machine_file_name)
        allocated_nodes = ["localhost\n"]
        try:
            machine_file = open(machine_file_name, "r")
            allocated_nodes = machine_file.readlines()
            machine_file.close()
        except:	
            traceback.print_exc(file=sys.stderr)

        logger.debug("Free nodes: " + str(allocated_nodes))         

        for i in allocated_nodes:
            logger.debug("free node: " + str(i) + " current busy nodes: " + str(self.busynodes) 
                          + " free nodes: " + str(self.freenodes))       
            self.busynodes.remove(i)
            self.freenodes.append(i)
        logger.debug("Delete " + machine_file_name)
        if os.path.exists(machine_file_name):
            os.remove(machine_file_name)
        self.resource_lock.release()
               
            
    def get_machine_file_name(self, job_dict):
        """create machinefile based on jobid"""
        job_id = job_dict["job-id"]                
        homedir = os.path.expanduser('~')
        return homedir  + "/advert-launcher-machines-"+ job_id
        
    def dequeue_new_jobs(self):	    
        """Subscribe to new jobs from Redis. """ 
        job_counter = 0               
        while self.is_stopped(self.base_url)==False:     
            if len(self.freenodes)==0:
                time.sleep(3)
                continue
            logger.debug("Dequeue sub-job from: " + self.base_url)       
            job_url=self.coordination.dequeue_job(self.base_url)
            logger.debug("Dequed:%s"%str(job_url))
            if job_url==None:
                time.sleep(3)
                continue
            if job_url=="STOP":
                break
            
            job_counter = job_counter + 1            
            if (job_counter % (THREAD_POOL_SIZE))==0: # ensure that threadpool is not too overloaded
                self.threadpool.wait()
            
            request = WorkRequest(self.start_new_job_in_thread, [job_url])
            self.threadpool.putRequest(request)
            
        # wait for termination of Worker Threads
        self.threadpool.wait()   
        logger.debug("Terminating Agent - Dequeue Sub-Jobs Thread")   
       
    #def poll_jobs(self):       
    #    self.threadpool.wait()
    #    new_jobs=self.redis.keys(self.base_url+":*")   
    #    logger.debug("All jobs:" + str(new_jobs))
    #    for i in new_jobs:            
    #        request = WorkRequest(self.start_new_job_in_thread, [str(i)])
    #        logger.debug("WorkRequest: " + str(request))
    #        self.threadpool.putRequest(request)
        
    def start_new_job_in_thread(self, job_url):
        """evaluates job dir, sanity checks, executes job """
        #pdb.set_trace()
        if job_url != None:
            failed = False;
            try:
                logger.debug("Get job description")
                job_dict = self.coordination.get_job(job_url)
            except:
                logger.error("Failed to get job description")
                failed=True
                
            if job_dict==None or failed==True:
                self.coordination.queue_job(self.pilot_url, job_url)
                
            logger.debug("start job: " + job_url + " data: " + str(job_dict))
            if(job_dict["state"]==str(bigjob.state.Unknown)):
                job_dict["state"]=str(bigjob.state.New)
                self.coordination.set_job_state(job_url, str(bigjob.state.New))
            self.execute_job(job_url, job_dict)
            #print "Execute: " + str(job_dict)
    
    def monitor_jobs(self):
        """Monitor running processes. """   
        #pdb.set_trace()
        logger.debug("Monitor jobs - # current jobs: %d"%len(self.jobs))
        for i in self.jobs:
            if self.processes.has_key(i): # only if job has already been starteds
                p = self.processes[i]
                p_state = p.poll()
                logger.debug(self.print_job(i) + " state: " + str(p_state) + " return code: " + str(p.returncode))
                if (p_state != None and (p_state==0 or p_state==255)):
                    logger.debug("Job successful: " + self.print_job(i) + " - set state to Done")
                    self.coordination.set_job_state(i, str(bigjob.state.Done))
                    self.free_nodes(i)
                    del self.processes[i]
                elif p_state!=0 and p_state!=255 and p_state != None:
                    logger.debug(self.print_job(i) + " failed.  ")
                    # do not free nodes => very likely the job will fail on these nodes
                    # self.free_nodes(i)                    
                    #if self.restarted.has_key(i)==False:
                    #    logger.debug("Try to restart job " + self.print_job(i))
                    #    self.restarted[i]=True
                    #    self.execute_job(i)                        
                    #else:
                    logger.debug("Job failed " + self.print_job(i))                    
                    self.coordination.set_job_state(i, str(bigjob.state.Failed))
                    self.free_nodes(i)
                    del self.processes[i]
    
    def print_job(self, job_url):
        job_dict = self.coordination.get_job(job_url)
        return  ("Job: " + job_url  + " Excutable: " + job_dict["Executable"])
                                
                            
    def start_background_thread(self):        
        self.stop=False                
        logger.debug("##################################### New POLL/MONITOR cycle ##################################")
        logger.debug("Free nodes: " + str(len(self.freenodes)) + " Busy Nodes: " + str(len(self.busynodes)))
        while True and self.stop==False:
            if self.is_stopped(self.base_url)==True:
                logger.debug("Pilot job entry deleted - terminate agent")
                break
            else:
                logger.debug("Pilot job entry: " + str(self.base_url) + " exists. Pilot job not in state stopped.")
            try:
                #self.poll_jobs()                
                self.monitor_jobs()            
                time.sleep(5)
                self.failed_polls=0
            except:
                traceback.print_exc(file=sys.stdout)
                self.failed_polls=self.failed_polls+1
                if self.failed_polls>3: # after 3 failed attempts exit
                    break
        logger.debug("Terminating Agent - Background Thread")
        
    
    def is_stopped(self, base_url):
        state = None
        try:
            state = self.coordination.get_pilot_state(base_url)
        except:
            pass
        logger.debug("Pilot State: " + str(state))
        if state==None or state.has_key("stopped")==False or state["stopped"]==True:
            return True
        else:
            return False
        

    def stop_background_thread(self):        
        self.stop=True
        
    
    def __get_launch_method(self, requested_method):
        """ returns desired execution method: ssh, aprun """
        
        aprun_available = False
        try:
            aprun_available = (subprocess.call("aprun -n 1 /bin/date", shell=True)==0)
        except:
            pass
        
        ssh_available = False
        try:
            ssh_available = (subprocess.call("ssh localhost /bin/date", shell=True)==0)
        except:
            pass
        
        launch_method = "ssh"
        if requested_method=="aprun" and aprun_available == True:
            launch_method="aprun"
        elif requested_method=="ssh" and ssh_available == True:
            launch_method="ssh"
        # aprun fallback
        elif ssh_available==False and aprun_available==True:
            launch_method="aprun"
        logger.debug("aprun: " + str(aprun_available) + " ssh: " + str(ssh_available) 
                     + " Launch method: " + str(launch_method))
        return launch_method
    
  
#########################################################
#  main                                                 #
#########################################################
if __name__ == "__main__" :
    args = sys.argv
    num_args = len(args)
    if (num_args!=3):
        print "Usage: \n " + args[0] + " <coordination host url> <coordination namespace url>"
        sys.exit(1)
    
    bigjob_agent = bigjob_agent(args)    
    
