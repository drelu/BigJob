""" Affinity-aware scheduler that evaluates affinity labels and input/output data flow
    

"""
import random
import pdb
import logging

class Scheduler:
    
    def __init__(self):
        self.pilot_data=[]
        self.pilot_jobs=[]
    
    def set_pilot_data(self, pilot_data):
        """ set resources which are used for scheduling """
        self.pilot_data=pilot_data
        
    
    def set_pilot_jobs(self, pilot_jobs):
        """ set resources which are used for scheduling """
        self.pilot_jobs=pilot_jobs
    
        
    def schedule_pilot_data(self, pilot_data_description=None):
        logging.debug("Schedule to PS - # Avail stores: %d"%len(self.pilot_data))     
        candidate_pilot_data = []  
        if pilot_data_description.has_key("affinity_datacenter_label") and pilot_data_description.has_key("affinity_machine_label"):
            for i in self.pilot_data: 
                pilot_store_description = i.pilot_store_description
                if pilot_store_description["affinity_datacenter_label"] == pilot_data_description["affinity_datacenter_label"]\
                and pilot_store_description["affinity_machine_label"] == pilot_data_description["affinity_machine_label"]:
                    candidate_pilot_data.append(i)
        else:
            candidate_pilot_data = self.pilot_data
            
        if len(candidate_pilot_data)>0:
            return random.choice(candidate_pilot_data)
        
        return None
        
        #if len(self.pilot_data)!=0:
        #    return random.choice(self.pilot_data)
        return None
    
    
    def schedule_pilot_job(self, work_unit_description=None):
        """ Enforces affinity description: if no PJ is available with the right
            affinity, WU can't be scheduled.
            
            TODO: incorporate potential data movements to co-locate PD/WU 
        
        """    
        logging.debug("Schedule to PJ - # Avail PJs: %d"%len(self.pilot_jobs))
        candidate_pilot_jobs = []
        if work_unit_description.has_key("affinity_datacenter_label") and work_unit_description.has_key("affinity_machine_label"):
            for i in self.pilot_jobs:
                pilot_job_description = i.pilot_compute_description
                if pilot_job_description["affinity_datacenter_label"] == work_unit_description["affinity_datacenter_label"]\
                and pilot_job_description["affinity_machine_label"] == work_unit_description["affinity_machine_label"]:
                    candidate_pilot_jobs.append(i)
        else:
            candidate_pilot_jobs=self.pilot_jobs
             
        if len(candidate_pilot_jobs)>0:
            return random.choice(candidate_pilot_jobs)
        
        return None
    
    def __check_pilot_data_dependency(self, work_unit_description):
        pilot_data_dependencies = work_unit_description["input_pilot_data"]
        for i in pilot_data_dependencies:
            pd = PilotData.pilot
            ps = i.get_pilot_data()