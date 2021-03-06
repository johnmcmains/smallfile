#!/usr/bin/python
'''
smallfile_cli.py -- CLI user interface for generating metadata-intensive workloads
Copyright 2012 -- Ben England
Licensed under the Apache License at http://www.apache.org/licenses/LICENSE-2.0
See Appendix on this page for instructions pertaining to license.
'''

# because it uses the "multiprocessing" python module instead of "threading"
# module, it can scale to many cores
# all the heavy lifting is done in "invocation" module,
# this script just adds code to run multi-process tests
# this script parses CLI commands, sets up test, runs it and prints results
#
# how to run:
#
# ./smallfile_cli.py 
#
import sys
import os
import os.path
import errno
import threading
import time
import socket
import string
import parse
import pickle
import math
import random
import shutil

# smallfile modules
import ssh_thread
import smallfile
from smallfile import smf_invocation, ensure_deleted, ensure_dir_exists, get_hostname, hostaddr
from smallfile import OK, NOTOK
import invoke_process
import sync_files
import output_results
import smf_test_params
import multi_thread_workload

# FIXME: should be monitoring progress, not total elapsed time
min_files_per_sec = 15 
pct_files_min = 70  # minimum percentage of files for valid test

# run a multi-host test

def run_multi_host_workload(prm):

    prm_host_set = prm.host_set
    prm_slave = prm.is_slave
    prm_permute_host_dirs = prm.permute_host_dirs
    master_invoke = prm.master_invoke

    starting_gate = master_invoke.starting_gate
    verbose = master_invoke.verbose
    host = master_invoke.onhost

    # construct list of ssh threads to invoke in parallel

    sync_files.create_top_dirs(master_invoke, True)
    pickle_fn = os.path.join(prm.master_invoke.network_dir,'param.pickle')
    #if verbose: print('writing ' + pickle_fn)
    sync_files.write_pickle(pickle_fn, prm)
    if os.getenv('PYPY'):
      python_prog = os.getenv('PYPY')
    elif sys.version.startswith('2'):
      python_prog = 'python'
    elif sys.version.startswith('3'):
      python_prog = 'python3'
    else:
      raise Exception('unrecognized python version %s'%sys.version)
    #print('python_prog = %s'%python_prog)
    ssh_thread_list = []
    host_ct = len(prm_host_set)
    for j in range(0, len(prm_host_set)):
        n = prm_host_set[j]
        this_remote_cmd = '%s %s/smallfile_remote.py --network-sync-dir %s '%\
           (python_prog, prm.remote_pgm_dir, prm.master_invoke.network_dir)
        
        #this_remote_cmd = remote_cmd
        if prm_permute_host_dirs:
          this_remote_cmd += ' --as-host %s'%prm_host_set[(j+1)%host_ct]
        else:
          this_remote_cmd += ' --as-host %s'%n
        if verbose: print(this_remote_cmd)
        ssh_thread_list.append(ssh_thread.ssh_thread(n, this_remote_cmd))

    # start them, pacing starts so that we don't get ssh errors

    for t in ssh_thread_list:
        t.start()

    # wait for hosts to arrive at starting gate
    # if only one host, then no wait will occur as starting gate file is already present
    # every second we resume scan from last host file not found
    # FIXME: for very large host sets, timeout only if no host responds within X seconds
  
    exception_seen = None
    hosts_ready = False  # set scope outside while loop
    abortfn = master_invoke.abort_fn()
    last_host_seen=-1
    sec = 0
    sec_delta = 0.5
    try:
     # FIXME: make timeout criteria be that new new hosts seen in X seconds
     while sec < prm.host_startup_timeout:
      os.listdir(master_invoke.network_dir)
      hosts_ready = True
      if os.path.exists(abortfn): raise Exception('worker host signaled abort')
      for j in range(last_host_seen+1, len(prm_host_set)-1):
        h=prm_host_set[j]
        fn = master_invoke.gen_host_ready_fname(h.strip())
        if verbose: print('checking for host filename '+fn)
        if not os.path.exists(fn):
            hosts_ready = False
            break
        last_host_seen=j
      if hosts_ready: break

      # be patient for large tests
      # give user some feedback about how many hosts have arrived at the starting gate

      time.sleep(sec_delta)
      sec += sec_delta
      sec_delta += 1
      if verbose: print('last_host_seen=%d sec=%d'%(last_host_seen,sec))
    except KeyboardInterrupt as e:
      print('saw SIGINT signal, aborting test')
      exception_seen = e
    except Exception as e:
      exception_seen = e
      hosts_ready = False
    if not hosts_ready:
      smallfile.abort_test(abortfn, [])
      if not exception_seen: 
        raise Exception('hosts did not reach starting gate within %d seconds'%prm.host_startup_timeout)
      else:
        print('saw exception %s, aborting test'%str(e))
    else:
      # ask all hosts to start the test
      # this is like firing the gun at the track meet
      try:
        sync_files.write_sync_file(starting_gate, 'hi')
        if verbose: print('starting gate file %s created'%starting_gate)
      except IOError as e:
        print('error writing starting gate: %s'%os.strerror(e.errno))

    # wait for them to finish

    all_ok = True
    for t in ssh_thread_list:
        t.join()
        if t.status != OK: 
          all_ok = False
          print('ERROR: ssh thread for host %s completed with status %d'%(t.remote_host, t.status))

    # attempt to aggregate results by reading pickle files
    # containing smf_invocation instances with counters and times that we need

    try:
      invoke_list = []
      for h in prm_host_set:  # for each host in test

        # read results for each thread run in that host
        # from python pickle of the list of smf_invocation objects

        pickle_fn = master_invoke.host_result_filename(h)
        if verbose: print('reading pickle file: %s'%pickle_fn)
        host_invoke_list = []
        try:
                if not os.path.exists(pickle_fn): time.sleep(1.2)
                with open(pickle_fn, 'rb') as pickle_file:
                  host_invoke_list = pickle.load(pickle_file)
                if verbose: print(' read %d invoke objects'%len(host_invoke_list))
                invoke_list.extend(host_invoke_list)
                ensure_deleted(pickle_fn)
        except IOError as e:
                if e.errno != errno.ENOENT: raise e
                print('  pickle file %s not found'%pickle_fn)

      output_results.output_results(invoke_list, prm_host_set, prm.thread_count,pct_files_min)

    except IOError as e:
        print('host %s filename %s: %s'%(h, pickle_fn, str(e)))
        all_ok = False
    except KeyboardInterrupt as e:
        print('control-C signal seen (SIGINT)')
        all_ok = False
    if not all_ok: 
        sys.exit(NOTOK)
    sys.exit(OK)


# main routine that does everything for this workload

def run_workload():
  # if a --host-set parameter was passed, it's a multi-host workload
  # each remote instance will wait until all instances have reached starting gate
   
  params = parse.parse()

  # for multi-host test

  if params.host_set and not params.is_slave:
    return run_multi_host_workload(params)
  return multi_thread_workload.run_multi_thread_workload(params)

# for future windows compatibility, all global code (not contained in a class or subroutine)
# must be moved to within a routine unless it's trivial (like constants)
# because windows doesn't support fork().

if __name__ == "__main__":
  run_workload()
