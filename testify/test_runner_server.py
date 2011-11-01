from test_runner import TestRunner
import tornado.httpserver
import tornado.ioloop
import tornado.web
try:
    import simplejson as json
    _hush_pyflakes = [json]
    del _hush_pyflakes
except ImportError:
    import json

import Queue
import time
import itertools

class AsyncQueue(object):
    def __init__(self):
        self.data_queue = Queue.PriorityQueue()
        self.callback_queue = Queue.PriorityQueue()
        self.finalized = False

    def get(self, priority, callback):
        if self.finalized:
            callback(None)
            return
        try:
            _, data = self.data_queue.get_nowait()
            callback(data)
        except Queue.Empty:
            self.callback_queue.put((priority, callback,))

    def put(self, priority, data):
        try:
            _, callback = self.callback_queue.get_nowait()
            callback(data)
        except Queue.Empty:
            self.data_queue.put((priority, data,))

    def empty(self):
        return self.data_queue.empty()

    def waiting(self):
        return self.callback_queue.empty()

    def finalize(self):
        """Call all queued callbacks with None, and make sure any future calls to get() immediately call their callback with None."""
        self.finalized = True
        try:
            while True:
                _, callback = self.callback_queue.get_nowait()
                callback(None)
        except Queue.Empty:
            pass

class TestRunnerServer(TestRunner):
    RUNNER_TIMEOUT = 300

    def __init__(self, *args, **kwargs):
        self.serve_port = kwargs.pop('serve_port')

        self.test_queue = AsyncQueue()
        self.checked_out = {} # Keyed on class path (module class).
        self.failed_rerun_methods = {} # Keyed on full method name (module class.method), values are results dicts.
        self.timeout_rerun_methods = set() # The set of all full method names that have timed out once.
        self.already_reported_methods = set() # The set of all full method names that we've reported already.
        super(TestRunnerServer, self).__init__(*args, **kwargs)

    def run(self):
        class DebugHandler(tornado.web.RequestHandler):
            def get(handler):
                trs = self
                import ipdb; ipdb.set_trace()

        class TestsHandler(tornado.web.RequestHandler):
            @tornado.web.asynchronous
            def get(handler):
                runner_id = handler.get_argument('runner')

                def callback(test_dict):
                    if test_dict:
                        self.check_out_class(runner_id, test_dict)

                        handler.finish(json.dumps({
                            'class': test_dict['class_path'],
                            'methods': test_dict['methods'],
                            'finished': False,
                        }))
                    else:
                        handler.finish(json.dumps({
                            'finished': True,
                        }))

                self.test_queue.get(0, callback)

        class ResultsHandler(tornado.web.RequestHandler):
            def post(handler):
                runner_id = handler.get_argument('runner')
                result = json.loads(handler.request.body)

                class_path = '%s %s' % (result['method']['module'], result['method']['class'])
                d = self.checked_out.get(class_path)

                if not d:
                    return handler.send_error(409, reason="Class %s not checked out." % class_path)
                if d['runner'] != runner_id:
                    return handler.send_error(409, reason="Class %s checked out by runner %s, not %s" % (class_path, d['runner'], runner_id))

                if result['success']:
                    d['passed_methods'][result['method']['name']] = result
                else:
                    d['failed_methods'][result['method']['name']] = result
                    self.failure_count += 1
                    if self.failure_limit and self.failure_count >= self.failure_limit:
                        self.early_shutdown()
                        return handler.finish("Too many failures, shutting down.")

                d['timeout_time'] = time.time() + self.RUNNER_TIMEOUT

                d['methods'].remove(result['method']['name'])
                if not d['methods']:
                    self.check_in_class(runner_id, class_path, finished=True)

                return handler.finish("kthx")

            def get_error_html(handler, status_code, **kwargs):
                reason = kwargs.pop('reason', None)
                if reason:
                    return reason
                else:
                    return super(ResultsHandler, handler).get_error_html()

        # Enqueue all of our tests.
        for test_dict in self.discover():
            test_case_class = test_dict['class']
            test_instance = test_case_class(
                suites_include=self.suites_include,
                suites_exclude=self.suites_exclude,
                suites_require=self.suites_require,
                name_overrides=test_dict['methods'])

            test_dict['class_path'] = '%s %s' % (test_case_class.__module__, test_case_class.__name__)
            test_dict['methods'] = [test.__name__ for test in test_instance.runnable_test_methods()]

            if test_dict['methods']:
                self.test_queue.put(0, test_dict)

        # Start an HTTP server.
        application = tornado.web.Application([
            (r"/tests", TestsHandler),
            (r"/results", ResultsHandler),
            (r"/debug", DebugHandler),
        ])

        server = tornado.httpserver.HTTPServer(application)
        server.listen(self.serve_port)
        tornado.ioloop.IOLoop.instance().start()

        report = [reporter.report() for reporter in self.test_reporters]
        return all(report)

    def check_out_class(self, runner, test_dict):
        self.checked_out[test_dict['class_path']] = {
            'runner' : runner,
            'class_path' : test_dict['class_path'],
            'methods' : set(test_dict['methods']),
            'failed_methods' : {},
            'passed_methods' : {},
            'timeout_time' : time.time() + self.RUNNER_TIMEOUT,
        }

        self.timeout_class(runner, test_dict['class_path'])

    def check_in_class(self, runner, class_path, timed_out=False, finished=False, early_shutdown=False):
        if 1 != len([opt for opt in (timed_out, finished, early_shutdown) if opt]):
            raise ValueError("Must set exactly one of timed_out, finished, or early_shutdown.")

        if class_path not in self.checked_out:
            raise ValueError("Class path %r not checked out." % class_path)
        if not early_shutdown and self.checked_out[class_path]['runner'] != runner:
            raise ValueError("Class path %r not checked out by runner %r." % (class_path, runner))

        d = self.checked_out.pop(class_path)

        for method, result_dict in itertools.chain(
                    d['passed_methods'].iteritems(),
                    ((method, result) for (method, result) in d['failed_methods'].iteritems() if early_shutdown or method in self.failed_rerun_methods),
                ):
            for reporter in self.test_reporters:
                result_dict['previous_run'] = self.failed_rerun_methods.get(method, None)
                reporter.test_start(result_dict)
                reporter.test_complete(result_dict)

        #Requeue failed tests
        requeue_dict = {
            'class_path' : d['class_path'],
            'methods' : [],
        }

        for method, result_dict in d['failed_methods'].iteritems():
            if method not in self.failed_rerun_methods:
                requeue_dict['methods'].append(method)
                self.failed_rerun_methods[method] = result_dict

        if finished:
            if len(d['methods']) != 0:
                raise ValueError("check_in_class called with finished=True but this class (%s) still has %d methods without results." % (class_path, len(d['methods'])))
        elif timed_out:
            # Requeue timed-out tests.
            for method in d['methods']:
                if method not in self.timeout_rerun_methods:
                    requeue_dict['methods'].append(method)
                    self.timeout_rerun_methods.add(method)

        if requeue_dict['methods']:
            self.test_queue.put(0, requeue_dict)

        if self.test_queue.empty() and len(self.checked_out) == 0:
            self.shutdown()

    def timeout_class(self, runner, class_path):
        """Check that it's actually time to rerun this class; if not, reset the timeout. Check the class in and rerun it."""
        d = self.checked_out.get(class_path, None)

        if not d:
            return

        if time.time() < d['timeout_time']:
            # We're being called for the first time, or someone has updated timeout_time since the timeout was set (e.g. results came in)
            tornado.ioloop.IOLoop.instance().add_timeout(d['timeout_time'], lambda: self.timeout_class(runner, class_path))
            return

        self.check_in_class(runner, class_path, timed_out=True)

    def early_shutdown(self):
        for class_path in self.checked_out.keys():
            self.check_in_class(None, class_path, early_shutdown=True)
        self.shutdown()

    def shutdown(self):
        # Can't immediately call stop, otherwise the current POST won't ever get a response.
        self.test_queue.finalize()
        iol = tornado.ioloop.IOLoop.instance()
        iol.add_timeout(time.time()+1, iol.stop)

