# Copyright (C) 2012 Allis Tauri <allista@gmail.com>
# 
# degen_primer is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# 
# degen_primer is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
# See the GNU General Public License for more details.
# 
# You should have received a copy of the GNU General Public License along
# with this program.  If not, see <http://www.gnu.org/licenses/>.
'''
Created on Jul 25, 2012

@author: Allis Tauri <allista@gmail.com>
'''

#imports
import os
import sys
import errno
import signal
from time import time, sleep
from datetime import timedelta
from Queue import Empty
from multiprocessing import Queue
from multiprocessing.managers import BaseManager
from threading import Thread
from contextlib import contextmanager
import TD_Functions
from PrimerTaskBase import PrimerTaskBase
from BlastPrimers import BlastPrimers
from SecStructures import AllSecStructures
from StringTools import hr, wrap_text, time_hr, print_exception
from SeqDB import SeqDB
from iPCR import iPCR
try:
    from Bio import SeqIO
except ImportError:
    print'The BioPython must be installed in your system.'
    raise
################################################################################ 


#managers for subprocessed classes
class ClassManager(BaseManager):
    def getpid(self):
        if '_process' in self.__dict__:
            return self._process.pid
        else: return None
#end class
ClassManager.register('AllSecStructures', AllSecStructures)
ClassManager.register('SeqDB', SeqDB)
ClassManager.register('iPCR', iPCR)
ClassManager.register('BlastPrimers', BlastPrimers)


class OutQueue(object):
    '''A file-like object which puts text written into it 
    in a cross-process queue'''
    def __init__(self, queue=None):
        if queue == None:
            self._queue = Queue()
        else: self._queue = queue
        
    @property
    def queue(self):
        return self._queue
        
    def write(self, text):
        self._queue.put(text)
    
    def flush(self): pass
#end class


class WaitThread(Thread):
    '''Thread that specially handle EOFError and IOError 4, interpreting them 
    as a signal that the target activity (which is supposed to run in another 
    process, e.g. in a Manager) has been terminated.'''
    def __init__(self, target=None, name=None, args=(), kwargs=dict()):
        Thread.__init__(self, name=name)
        self._target = target
        self._args   = args
        self._kwargs = kwargs
        
    def run(self):
        try:
            self._target(*self._args, **self._kwargs)
        #Ctrl-C
        except KeyboardInterrupt: return
        #EOF means that target activity was terminated in the Manager process
        except EOFError: return
        #IO code=4 means the same
        except IOError, e:
            if e.errno == errno.EINTR:
                return
            elif e.errno == errno.EBADMSG:
                print '\nError in thread: %s' % self.name
                print e.message
                print '\n*** It seems that an old degen_primer_gui ' \
                'subprocess is running in the system. Kill it and try again. ***\n'
                return
            else:
                print '\nError in thread: %s' % self.name
                print print_exception(e)
                return
        except Exception, e:
            print '\nError in thread: %s' % self.name
            print print_exception(e)
            return
    #end def
#end class


#a context manager to capture output of print into OutQueue object
@contextmanager
def capture_to_queue(out_queue=None):
    oldout,olderr = sys.stdout, sys.stderr
    try:
        out = OutQueue(out_queue)
        #need to leave stderr due to the http://bugs.python.org/issue14308
        sys.stdout = out #sys.stderr = out
        yield out
    except Exception, e:
        print_exception(e)
        raise
    finally:
        sys.stdout,sys.stderr = oldout, olderr
#end def


#subprocess worker factory and launcher
def _subroutine(func, args, queue, p_entry, p_name=None):
    '''
    Run a func in a subrocess with args provided as dictionary.
    Output will be put into a queue q.
    Return Process object. 
    '''
    routine_name = p_name if p_name else func.__name__
     
    def worker(_args, _q):
        #remember start time
        time0 = time()
        #run the function
        if _args is None: func()
        else: func(*_args)
        _q.put('\nTask has finished:\n   %s\nElapsed time: %s' % (routine_name, timedelta(seconds=time()-time0)))
    #end def
    
    #start subprocess
    subroutine = WaitThread(target=worker, args=[args, queue], name=routine_name)
    subroutine.daemon = True
    p_entry['process'] = subroutine
    print '\nStarting CPU-intensive task:\n   %s\nThis may take awhile...' % routine_name
    subroutine.start()
#end def


class AnalysisTask(PrimerTaskBase):
    '''This class gathers all calculations in a single pipeline with 
    parallelized subroutines for CPU-intensive computations.'''
    
    #file format for saving primers
    _fmt = 'fasta'
    

    def __init__(self):
        PrimerTaskBase.__init__(self)
        self._subroutines = [] #list of launched processes with their managers and output queues
        self._terminated  = False #flag which is set when pipeline is terminated
    #end def


    def terminate(self):
        if self._terminated: return
        self._terminated = True
        #try to shutdown managers
        for p_entry in self._subroutines:
            try: 
                p_entry['manager'].shutdown()
                if p_entry['children']:
                    for child in p_entry['children']:
                        os.kill(child, signal.SIGTERM)
                        sleep(0.1) #control shot
                        os.kill(child, signal.SIGKILL)
            except: pass
        self._subroutines = []
    #end def


    def _generate_subroutine_entry(self, queue):
        p_entry = {'process'  : None,
                   'children' : [],
                   'manager'  : ClassManager(),
                   'queue'    : queue,
                   'output'   : []}
        self._subroutines.append(p_entry)
        return p_entry
    #end def


    def run(self, args):
        #reset global state of the module
        self._terminated  = False
        self._subroutines = []

        #zero time, used to calculate elapsed time in the end
        time0 = time()
        
        #set PCR parameters
        TD_Functions.PCR_P.set(args.options)
        
        #calculate primers' Tm
        for primer in args.primers: primer.calculate_Tms()
        
        #save primers 
        if not self._save_primers(args.primers): return False
        #----------------------------------------------------------------------#
    
        #following computations are CPU intensive, so they need parallelization#
        #check primers for hairpins, dimers and cross-dimers
        with capture_to_queue() as out:
            p_entry = self._generate_subroutine_entry(out.queue)
            p_entry['manager'].start()
        all_sec_structures  = p_entry['manager'].AllSecStructures(args.primers)
        side_reactions      = all_sec_structures.reactions()
        side_concentrations = all_sec_structures.concentrations()
        _subroutine(all_sec_structures.calculate_equilibrium, None, out.queue, p_entry,
                    'Calculate conversion degree of secondary structures.')
        #----------------------------------------------------------------------#
        
        
        #in silica PCR simulation. This is only available if sequence database is provided in some form 
        ipcr = None
        if args.fasta_files or args.sequence_db:
            with capture_to_queue() as out:
                p_entry = self._generate_subroutine_entry(out.queue)
                p_entry['manager'].start()
            ipcr = p_entry['manager'].iPCR(args.max_mismatches,
                                           args.job_id, 
                                           args.primers, 
                                           args.min_amplicon, 
                                           args.max_amplicon, 
                                           args.polymerase, 
                                           args.with_exonuclease, 
                                           args.cycles,
                                           side_reactions, 
                                           side_concentrations,
                                           args.analyse_all_annealings)
            #connect to sequence database
            seq_files = []
            if args.sequence_db: seq_files.append(args.sequence_db)
            else: seq_files = args.fasta_files
            _subroutine(ipcr.find_and_analyse, 
                        (seq_files, 
                         args.use_sequences), out.queue, p_entry,
                        'Simulate PCR using possible products found in provided sequences.')
        #----------------------------------------------------------------------#
        
        
        #test for primers specificity by BLAST
        with capture_to_queue() as out:
            p_entry = self._generate_subroutine_entry(out.queue)
            p_entry['manager'].start()
        blast_primers = p_entry['manager'].BlastPrimers(args.job_id, 
                                                        args.primers, 
                                                        args.min_amplicon, 
                                                        args.max_amplicon, 
                                                        args.polymerase, 
                                                        args.with_exonuclease, 
                                                        args.cycles,
                                                        side_reactions, 
                                                        side_concentrations,
                                                        include_side_annealings=args.analyse_all_annealings)
        #if --do-blast flag was provided, make an actual query
        if args.do_blast:
            #construct Entrez query
            entrez_query = ''
            if args.organisms:
                for organism in args.organisms:
                    if entrez_query: entrez_query += ' OR '
                    entrez_query += organism+'[organism]'
            #do the blast and analyze the results
            _subroutine(blast_primers.blast_and_analyze, 
                        (entrez_query,),
                        out.queue, p_entry,
                        'Make BLAST query, then simulate PCR using returned alignments.')
        #else, try to load previously saved results and analyze them with current parameters
        elif blast_primers.load_results():
            print '\nFound saved BLAST results.'
            _subroutine(blast_primers.simulate_PCR, 
                        None, 
                        out.queue, p_entry,
                        'Simulate PCR using alignments in BLAST results.')
        else: self._print_queue(p_entry['queue'])
        #----------------------------------------------------------------------#
        
        
        #collect subroutines output, write it to stdout, wait for them to finish
        self._listen_subroutines()
        #if subroutines have been terminated, abort pipeline
        if self._terminated:
            print '\nDegenPrimer Pipeline aborted.' 
            return -1
        #----------------------------------------------------------------------#
        
        
        #---------------------now write all the reports------------------------#
        #write full and short reports
        print ''
        structures_full_report_filename  = args.job_id+'-full-report.txt'
        structures_short_report_filename = args.job_id+'-short-report.txt'
        try:
            full_structures_file  = open(structures_full_report_filename, 'w')
            short_structures_file = open(structures_short_report_filename, 'w')
        except IOError, e:
            print '\nUnable to open report file(s) for writing.'
            print_exception(e)
        else:
            #write header
            for f in (full_structures_file, short_structures_file):
                f.write(self._format_primers_report_header(args.primers, args.polymerase))
            #write secondary structures information
            full_structures_file.write(all_sec_structures.print_structures())
            short_structures_file.write(all_sec_structures.print_structures_short())
            full_structures_file.close()
            short_structures_file.close()
            print '\nFull report with all secondary structures was written to:\n   ',structures_full_report_filename
            print '\nShort report with a summary of secondary structures was written to:\n   ',structures_short_report_filename
            args.register_report('Tm and secondary structures', structures_short_report_filename)
                
        #write iPCR report if it is available
        if ipcr != None and ipcr.have_results():
            ipcr.write_products_report()
            ipcr.write_report()
            for report in ipcr.reports():
                args.register_report(**report)
        
        #write BLAST reports
        if blast_primers.have_results():
            blast_primers.write_hits_report()
            blast_primers.write_report()
            for report in blast_primers.reports():
                args.register_report(**report)
                    
        #print last queued messages
        sleep(0.1)
        for p_entry in self._subroutines:
            self._print_queue(p_entry['queue'])
#        #----------------------------------------------------------------------#
        
        #terminate managers and delete queues
        self.terminate()
        
        print '\nDone. Total elapsed time: %s' % timedelta(seconds=time()-time0)
        return 1
    #end def


    @classmethod
    def _save_primers(cls, primers):
        for primer in primers:
            if not primer: continue
            filename = primer.id+'.'+cls._fmt
            try:
                SeqIO.write(primer.all_seq_records, filename, cls._fmt)
            except Exception, e:
                print '\nFailed to write %s primer and it\'s unambiguous components to:\n   %s' % (primer.id, filename)
                print_exception(e)
                return False 
            print '\n%s primer and it\'s unambiguous components were written to:\n   %s' % (primer.id, filename)
        return True
    #end def
    
    
    @staticmethod
    def _print_queue(queue):
        output = []
        while True:
            try: output.append(queue.get(False))
            except Empty:
                print ''.join(message for message in output)
                return
    #end def
                
    
    def _listen_subroutines(self):
        '''While waiting for their termination, get output from subroutines. 
        When some subroutine has finished, print out it's output.'''
        while not self._terminated:
            processes_alive = False
            for p_entry in self._subroutines:
                if p_entry['process'] == None:
                    continue
                try:
                    while True: p_entry['output'].append(p_entry['queue'].get(False))
                except Empty:
                    if p_entry['process'].is_alive():
                        processes_alive = True
                    else:
                        p_entry['process'].join()
                        p_entry['process'] = None
                        print ''.join(unicode(message) for message in p_entry['output'])
                    continue
                except IOError, e:
                    if e.errno == errno.EINTR:
                        processes_alive = True
                        continue
                    else:
                        print '\nAnalysisTask._listen_subroutines:'
                        print_exception(e)
                        self.terminate()
                except Exception, e:
                    print '\nAnalysisTask._listen_subroutines:'
                    print_exception(e)
                    self.terminate()
                processes_alive = True
            if not processes_alive: break
            sleep(0.1)
    #end def
    
    
    @staticmethod
    def _format_primers_report_header(primers, polymerase):
        header_string  = ''
        header_string += time_hr()
        header_string += wrap_text('For each degenerate primer provided, a set '
                                   'of unambiguous primers is generated. '
                                   'For each such set the minimum, maximum and '
                                   'mean melting temperatures are calculated. '
                                   'For each primer in each set stable self-'
                                   'dimers and hairpins are predicted. '
                                   'For every possible combination of two '
                                   'unambiguous primers cross-dimers are also '
                                   'predicted. If an unambiguous primer is '
                                   'provided, it is treated as a set with a '
                                   'single element.\n\n')
        header_string += hr(' PCR conditions ')
        header_string += TD_Functions.format_PCR_conditions(primers, polymerase)+'\n'
        header_string += hr(' primers and their melting temperatures ')
        for primer in primers:
            header_string += repr(primer) + '\n'
        #warning
        if len(primers) > 1:
            if abs(primers[0].Tm_min - primers[1].Tm_min) >= 5:
                header_string += '\nWarning: lowest melting temperatures of sense and antisense primes \n'
                header_string += '         differ more then by 5C\n'
        header_string += '\n'
        return header_string
#end class