'''
strategy.py

Copyright 2006 Andres Riancho

This file is part of w3af, w3af.sourceforge.net .

w3af is free software; you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation version 2 of the License.

w3af is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with w3af; if not, write to the Free Software
Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA

'''
import Queue

from multiprocessing import TimeoutError

import core.data.kb.knowledgeBase as kb
import core.data.kb.config as cf
import core.controllers.outputManager as om

from core.data.request.fuzzable_request import FuzzableRequest

from core.controllers.core_helpers.consumers.grep import grep
from core.controllers.core_helpers.consumers.auth import auth
from core.controllers.core_helpers.consumers.audit import audit
from core.controllers.core_helpers.consumers.bruteforce import bruteforce
from core.controllers.core_helpers.consumers.seed import seed
from core.controllers.core_helpers.consumers.crawl_infrastructure import crawl_infrastructure 
from core.controllers.core_helpers.consumers.constants import POISON_PILL
from core.controllers.core_helpers.exception_handler import ExceptionData

from core.controllers.w3afException import w3afMustStopException


class w3af_core_strategy(object):
    '''
    This is the simplest scan strategy which follows this logic:
        
        while new_things_found():
            discovery()
            bruteforce()
        audit(things)
    
    It has been w3af's main algorithm for a while, and what we want to do now
    is to decouple it from the core in order to make experiments and implement
    new algorithms that are more performant.
    
    Use this strategy as a base for your experiments!
    '''
    def __init__(self, w3af_core):
        self._w3af_core = w3af_core
        
        # Internal attribute to keep track of all known fuzzable requests
        # TODO: Make this a disk_set to reduce memory usage.
        self._fuzzable_request_set = set()
        kb.kb.save('urls', 'fuzzable_requests', self._fuzzable_request_set)
        
        # Consumer threads
        self._grep_consumer = None
        self._audit_consumer = None
        self._auth_consumer = None
        
        # Producer/consumer threads
        self._discovery_consumer = None
        self._bruteforce_consumer = None
        
        # Producer threads
        self._seed_producer = seed(self._w3af_core)
        
        self._consumers = [self._grep_consumer,
                           self._audit_consumer,
                           self._auth_consumer,
                           self._discovery_consumer,
                           self._bruteforce_consumer]

    def start(self):
        '''
        Starts the work!
        User interface coders: Please remember that you have to call 
        core.plugins.init_plugins() method before calling start.
        
        @return: No value is returned.
        '''
        try:
            self._setup_grep()
            self._setup_auth()
            self._setup_crawl_infrastructure()
            self._setup_audit()
            self._setup_bruteforce()
            self._setup_404_detection()
            
            self._seed_discovery()
            
            self._fuzzable_request_router()
            
            self.join_all_consumers()
            
        except Exception:
            self.terminate()
            raise

    def stop(self):
        self.terminate()
    
    quit = stop
    
    def pause(self, pause_yes_no):
        # FIXME: Consumers should have something to do with this, most likely
        # another constant similar to the poison pill
        pass

    def terminate(self):
        '''
        Consume (without processing) all queues with data which are in
        the consumers and then send a poison-pill to that queue.
        '''
        for consumer in self._consumers:
            if consumer is not None:
                consumer.terminate()
    
    def join_all_consumers(self):
        '''
        End the strategy specific things.
        '''
        self._teardown_grep()
        self._teardown_audit()
        self._teardown_auth()
        self._teardown_crawl_infrastructure()
        self._teardown_bruteforce()
    
    def _fuzzable_request_router(self):
        '''
        This is one of the most important methods, it will take things from the
        discovery Queue and store them in one or more Queues (audit, bruteforce,
        etc).
        
        Also keep in mind that is one of the only methods that will be run in
        the "main thread" and lives during the whole scan process.
        '''
        _input = [self._seed_producer, self._discovery_consumer, 
                  self._bruteforce_consumer]
        _input = filter(None, _input)
       
        output = [self._audit_consumer, self._discovery_consumer,
                  self._bruteforce_consumer]
        output = filter(None, output)
        
        # Only check if these have exceptions and bring them to the main
        # thread in order to be handled by the ExceptionHandler and the
        # w3afCore
        _other = [self._auth_consumer, self._grep_consumer]
        _other = filter(None, _other)
        
        finished = set()
        consumer_forced_end = set()
        
        
        while True:
            # Get results and handle exceptions
            self._handle_all_consumer_exceptions(_other)

            # Route fuzzable requests
            route_result = self._route_one_fuzzable_request_batch(_input, output,
                                                                  finished,
                                                                  consumer_forced_end)
            
            if route_result is None:
                break
            
            finished, consumer_forced_end = route_result
                                      
    def _route_one_fuzzable_request_batch(self, _input, output, finished,
                                          consumer_forced_end):
        '''
        Loop once through all input consumers and route their results.
        
        @return: (finished, consumer_forced_end) or None if we shouldn't call
                 this method anymore.
        '''
        for url_producer in _input:
            
            if len(_input) == len(finished | consumer_forced_end):
                return
            
            if url_producer in finished:
                continue
            
            try:
                result_item = url_producer.get_result(timeout=0.2)
            except TimeoutError:
                pass
            
            except Queue.Empty:
                if not url_producer.has_pending_work():
                    # This consumer is saying that it doesn't have any
                    # pending or in progress work
                    finished.add( url_producer )
            else:
                if result_item == POISON_PILL:
                    # This consumer is saying that it has finished, so we
                    # remove it from the list.
                    consumer_forced_end.add(url_producer)
                elif isinstance(result_item, ExceptionData):
                    self._handle_consumer_exception(result_item)
                else:
                    _, _, fuzzable_request_inst = result_item
                    
                    # Safety check, I need these to be FuzzableRequest objects
                    # if not, the url_producer is doing something wrong and I
                    # don't want to do anything with this data
                    fmt = '%s is returning objects of class %s instead of FuzzableRequest.'
                    msg = fmt % (url_producer, type(fuzzable_request_inst))
                    assert isinstance(fuzzable_request_inst, FuzzableRequest), msg
                    
                    for url_consumer in output:
                        url_consumer.in_queue_put( fuzzable_request_inst )
                    
                    # This is rather complex to digest... so pay attention :)
                    # A consumer might be 100% idle (no tasks in input or 
                    # output queues, no in progress work) and we still need
                    # to keep it alive, because output from another producer
                    # will be sent to that consumer and make it work again
                    #
                    # So, when one producer returns something, we set the
                    # finished list to empty in order to make them work again
                    finished = set()
        
        return finished, consumer_forced_end

    def _handle_all_consumer_exceptions(self, _other):
        '''
        Get the exceptions raised by the consumers that do not return any
        data under normal circumstances, for example: grep and auth and handle
        them properly.
        '''
        for other_consumer in _other:
            try:
                result_item = other_consumer.get_result(timeout=0.2)
            except TimeoutError:
                pass
            except Queue.Empty:
                pass
            else:
                if isinstance(result_item, ExceptionData):
                    self._handle_consumer_exception(result_item)
    
    def _handle_consumer_exception(self, exception_data):
        '''
        Give proper handling to an exception that was raised by one of the
        consumers. Usually this means calling the ExceptionHandler which
        will decide what to do with it.
        
        Please note that xUrllib can raise a w3afMustStopByUserRequest which
        should get through this piece of code and be re-raised in order to
        reach the try/except clause in w3afCore's start.
        '''
        self._w3af_core.exception_handler.handle_exception_data(exception_data)
        
    def _setup_404_detection(self):
        #
        #    NOTE: I need to perform this test here in order to avoid some weird
        #    thread locking that happens when the webspider calls is_404, and
        #    because I want to initialize the is_404 database in a controlled
        #    try/except block.
        #
        from core.controllers.core_helpers.fingerprint_404 import is_404
        
        for url in cf.cf.get('targets'):
            try:
                response = self._w3af_core.uri_opener.GET(url, cache=True)
                is_404(response)
            except Exception, e:
                msg = 'Failed to initialize the 404 detection, original exception'
                msg += ' was: "%s".'                
                raise w3afMustStopException(msg % e)
                        
    def _setup_crawl_infrastructure(self):
        '''
        Setup the crawl and infrastructure consumer:
            * Retrieve all plugins from the core,
            * Create the consumer instance and more,
        '''
        crawl_plugins = self._w3af_core.plugins.plugins['crawl']
        infrastructure_plugins = self._w3af_core.plugins.plugins['infrastructure']
        
        if crawl_plugins or infrastructure_plugins:
            discovery_plugins = crawl_plugins
            discovery_plugins.extend(infrastructure_plugins)
            
            self._discovery_consumer = crawl_infrastructure(discovery_plugins,
                                                            self._w3af_core,
                                                            cf.cf.get('maxDiscoveryTime'))
            self._discovery_consumer.start()
    
    def _setup_grep(self):
        '''
        Setup the grep consumer:
            * Create a Queue,
            * Set the Queue in xurllib
            * Start the consumer
        '''
        grep_plugins = self._w3af_core.plugins.plugins['grep']
        
        if grep_plugins:
            self._grep_consumer = grep(grep_plugins, self._w3af_core)
            self._w3af_core.uri_opener.set_grep_queue_put( self._grep_consumer.in_queue_put )
            self._grep_consumer.start()
        
    def _teardown_grep(self):
        if self._grep_consumer is not None: 
            self._grep_consumer.join()
            self._grep_consumer = None
    
    def _teardown_audit(self):
        if self._audit_consumer is not None:
            # Wait for all the in_queue items to get() from the queue
            self._audit_consumer.join()
            self._audit_consumer = None
        
    def _teardown_auth(self):
        if self._auth_consumer is not None:
            self._auth_consumer.join()
            self._auth_consumer = None
    
    def _teardown_bruteforce(self):
        if self._bruteforce_consumer is not None:
            self._bruteforce_consumer.join()
            self._bruteforce_consumer = None
                
    def _teardown_crawl_infrastructure(self):
        if self._discovery_consumer is not None:
            self._discovery_consumer.join()
            self._discovery_consumer = None
              
    def _seed_discovery(self):
        '''
        Create the first fuzzable request objects based on the targets and put
        them in the crawl_infrastructure consumer Queue.
        
        This will start the whole discovery process, since plugins are going
        to consume from that Queue and then put their results in it again in 
        order to continue discovering.
        '''
        #
        #    GET the initial target URLs in order to save them
        #    in a list and use them as our bootstrap URLs
        #
        self._seed_producer.seed_output_queue( cf.cf.get('targets') )
    
    def _setup_bruteforce(self):
        '''
        Create a bruteforce consumer instance with the bruteforce plugins
        and initialize it in order to start taking work from the input Queue.
        
        The input queue for this consumer is populated by the fuzzable request
        router.
        '''
        bruteforce_plugins = self._w3af_core.plugins.plugins['bruteforce']
        
        if bruteforce_plugins:
            self._bruteforce_consumer = bruteforce(bruteforce_plugins,
                                                   self._w3af_core)
            self._bruteforce_consumer.start()
    
    def force_auth_login(self):
        '''Force a login in a sync way
        @return: None.
        '''
        if self._auth_consumer is not None:
            self._auth_consumer.force_login()
    
    def _setup_auth(self, timeout=5):
        '''
        Start the thread that will make sure the xurllib always has a "fresh"
        session. The thread will call is_logged() and login() for each enabled
        auth plugin every "timeout" seconds.
        
        If there is a specific need to make sure that the session is fresh before
        performing any step, the developer needs to run the force_auth_login()
        method.
        '''
        auth_plugins = self._w3af_core.plugins.plugins['auth']
        
        if auth_plugins:
            self._auth_consumer = auth(auth_plugins, self._w3af_core, timeout)
            self._auth_consumer.start()
            self._auth_consumer.force_login()
                                
    def _setup_audit(self):
        '''
        Starts the audit plugin consumer 
        '''
        om.out.debug('Called _setup_audit()' )

        audit_plugins = self._w3af_core.plugins.plugins['audit']
        
        if audit_plugins:
            self._audit_consumer = audit(audit_plugins, self._w3af_core)
            self._audit_consumer.start()
            