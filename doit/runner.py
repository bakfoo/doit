"""Task runner."""

import sys

from multiprocessing import Process, Queue

from doit.exceptions import CatchedException
from doit.exceptions import TaskFailed, SetupError, DependencyError
from doit.dependency import Dependency
from doit.task import Task

class SetupManager(object):
    """Manage setup objects

    Setup object is any object that implements 'setup' and/or 'cleanup'
    @ivar _loaded (list): of loaded setup objects
    """

    def __init__(self):
        self._loaded = set()


    def load(self, setup_obj):
        """run setup from a setup_obj if it is not loaded yet"""
        if setup_obj in self._loaded:
            return

        self._loaded.add(setup_obj)
        if hasattr(setup_obj, 'setup'):
            setup_obj.setup()


    def cleanup(self):
        """run cleanup for all loaded objects"""
        for setup_obj in self._loaded:
            if hasattr(setup_obj, 'cleanup'):
                try:
                    setup_obj.cleanup()
                # report error but keep result as successful.
                # FIXME should execute all cleanup's even with errors
                # TODO caller should handle the exception
                except Exception, e:
                    return SetupError("ERROR on setup_obj cleanup", e)


# execution result.
SUCCESS = 0
FAILURE = 1
ERROR = 2

class Runner(object):
    """Task runner

    run_tasks():
        for each task:
            select_task()
            execute_task()
            process_task_result()
    finish()

    """
    def __init__(self, dependencyFile, reporter, continue_=False,
                 always_execute=False, verbosity=0):
        """@param dependencyFile: (string) file path of the db file
        @param reporter: reporter to be used. It can be a class or an object
        @param continue_: (bool) execute all tasks even after a task failure
        @param always_execute: (bool) execute even if up-to-date or ignored
        @param verbosity: (int) 0,1,2 see Task.execute
        """
        self.dependencyManager = Dependency(dependencyFile)
        self.reporter = reporter
        self.continue_ = continue_
        self.always_execute = always_execute
        self.verbosity = verbosity

        self.setupManager = SetupManager()
        self.teardown_list = [] # list of tasks to be teardown
        self.final_result = SUCCESS # until something fails
        self._stop_running = False


    def _handle_task_error(self, task, catched_excp):
        assert isinstance(catched_excp, CatchedException)
        self.dependencyManager.remove_success(task)
        self.reporter.add_failure(task, catched_excp)
        # only return FAILURE if no errors happened.
        if isinstance(catched_excp, TaskFailed):
            self.final_result = FAILURE
        else:
            self.final_result = ERROR
        if not self.continue_:
            self._stop_running = True


    def select_task(self, task):
        """Returns bool, task should be executed
         * side-effect: set task.options
        """

        # check if run_status was already calculated
        if task.run_status is None:
            # TODO reporter.start_task rename to get_status
            self.reporter.start_task(task)
            # check if task is up-to-date
            try:
                task.run_status = self.dependencyManager.get_status(task)
            except Exception, exception:
                de = DependencyError("ERROR checking dependencies", exception)
                self._handle_task_error(task, de)
                return False

            if not self.always_execute:
                # if task is up-to-date skip it
                if task.run_status == 'up-to-date':
                    self.reporter.skip_uptodate(task)
                    return False
                # check if task should be ignored (user controlled)
                if task.run_status == 'ignore':
                    self.reporter.skip_ignore(task)
                    return False

            if task.setup_tasks:
                # dont execute now, execute setup first...
                return False
        else:
            assert task.run_status == 'run', "%s:%s" % (task.name, task.run_status)
            # check if already executed
            if not task.setup_tasks:
                return False

        # selected just need to get values from other tasks
        for arg, value in task.getargs.iteritems():
            try:
                task.options[arg] = self.dependencyManager.get_value(value)
            except Exception, exception:
                msg = ("ERROR getting value for argument '%s'\n" % arg +
                       str(exception))
                self._handle_task_error(task, DependencyError(msg))
                return False

        return True


    def execute_task(self, task):
        """execute task's actions"""
        # setup env
        for setup_obj in task.setup:
            try:
                self.setupManager.load(setup_obj)
            except (SystemExit, KeyboardInterrupt): raise
            except Exception, exception:
                return SetupError("ERROR on object setup", exception)

        # new style cleanup/teardown
        if task.teardown:
            self.teardown_list.append(task)

        # finally execute it!
        self.reporter.execute_task(task)
        return task.execute(sys.stdout, sys.stderr, self.verbosity)


    def process_task_result(self, task, catched_excp):
        # save execution successful
        if catched_excp is None:
            self.dependencyManager.save_success(task)
            self.reporter.add_success(task)
        # task error
        else:
            self._handle_task_error(task, catched_excp)


    def run_tasks(self, task_control):
        """This will actually run/execute the tasks.
        It will check file dependencies to decide if task should be executed
        and save info on successful runs.
        It also deals with output to stdout/stderr.

        @param task_control: L{TaskControl}
        """
        for task in task_control.get_next_task():
            if self._stop_running:
                break
            if not self.select_task(task):
                continue
            catched_excp = self.execute_task(task)
            self.process_task_result(task, catched_excp)


    def teardown(self):
        """run teardown from all tasks"""
        for task in self.teardown_list:
            self.reporter.teardown_task(task)
            catched = task.execute_teardown(sys.stdout, sys.stderr,
                                            self.verbosity)
            if catched:
                msg = "ERROR: task '%s' teardown action" % task.name
                error = SetupError(msg, catched)
                self.reporter.cleanup_error(error)


    def finish(self):
        """finish running tasks"""
        # flush update dependencies
        self.dependencyManager.close()

        # clean setup objects
        error = self.setupManager.cleanup()
        if error:
            self.reporter.cleanup_error(error)

        # new style teardown
        self.teardown()

        # report final results
        self.reporter.complete_run()
        return self.final_result


class Hold(object):
    """Sentinel class: No task ready to be executed"""
    pass

class MP_Runner(Runner):
    """MultiProcessing Runner """

    class MP_Reporter(object):
        """send reported messages to master process

        puts a dictionary {'name': <task-name>,
                           'reporter': <reporter-method-name>}
        on runner's 'result_q'
        """
        def __init__(self, runner, original_reporter):
            self.runner = runner
            self.original_reporter = original_reporter

        def __getattr__(self, method_name):
            """substitute any reporter method with a dispatching method"""
            if not hasattr(self.original_reporter, method_name):
                raise AttributeError(method_name)
            def rep_method(task):
                self.runner.result_q.put({'name':task.name,
                                          'reporter':method_name})
            return rep_method


    def __init__(self, dependencyFile, reporter, continue_=False,
                 always_execute=False, verbosity=0, num_process=1):
        Runner.__init__(self, dependencyFile, reporter, continue_,
                        always_execute, verbosity)
        self.num_process = num_process
        self.waiting = {}
        self.ready_queue = []
        self.free_proc = 0
        self.task_gen = None
        self.tasks = None

    def get_next_task(self):
        """get next task to be dispatched to sub-process

        On MP needs to check if the dependencies finished its execution
        @returns: - a task
                  - None -> no more tasks to be executed
                  - Hold object, all tasks are waiting for dependencies
        """
        if self._stop_running:
            return None # gentle stop

        def nothing_ready():
            if self.waiting:
                self.free_proc += 1
                return Hold()
            else:
                return None

        while True:
            # get new task
            if self.ready_queue:
                task_name = self.ready_queue.pop(0)
                task = self.tasks[task_name]
            else:
                try:
                    task = self.task_gen.next()
                    if not isinstance(task, Task):
                        self.free_proc += 1
                        return Hold()
                except StopIteration:
                    return nothing_ready()

            # check task-dependencies are done
            for dep in task.task_dep + task.setup_tasks:
                if self.tasks[dep].run_status == 'run':
                    if dep in self.waiting:
                        self.waiting[dep].append(task.name)
                    else:
                        self.waiting[dep] = [task.name]
                    break
            # dont need to wait for another task
            else:
                if self.select_task(task):
                    return task

    def set_tasks(self, task_control):
        self.task_gen = task_control.get_next_task()
        self.tasks = task_control.tasks

    def _finished_running_task(self, task):
        task.run_status = "done"
        if task.name in self.waiting:
            for ready_task in self.waiting[task.name]:
                self.ready_queue.append(ready_task)
            del self.waiting[task.name]

    def run_tasks(self, task_control):
        result_q = Queue()
        task_q = Queue()
        proc_list = []
        self.set_tasks(task_control)

        # create and start processes
        for p_id in xrange(self.num_process):
            next_task = self.get_next_task()
            if next_task is None:
                break # do not start more processes than tasks
            task_q.put(next_task)
            process = Process(target=self.execute_task,
                              args=(task_q, result_q))
            process.start()
            proc_list.append(process)

        # wait for all processes terminate
        proc_count = len(proc_list)
        while proc_count:
            result = result_q.get()
            task = task_control.tasks[result['name']]
            if 'reporter' in result:
                getattr(self.reporter, result['reporter'])(task)
                continue
            elif 'failure' in result:
                catched_excp = result['failure']
            elif 'exit' in result:
                raise result['exit'](result['exception'])
            else:
                catched_excp = None
                task.result = result['result']
                task.values = result['values']

            # completed one task, dispatch next one
            self.process_task_result(task, catched_excp)
            self._finished_running_task(task)

            free_proc = self.free_proc
            self.free_proc = 0
            for get_one_more in range(1 + free_proc):
                next_task = self.get_next_task()
                if next_task is None:
                    proc_count -= 1
                task_q.put(next_task)

        # we are done, join all process
        for proc in proc_list:
            proc.join()

        # get teardown results
        while not result_q.empty(): # safe because subprocess joined
            result = result_q.get()
            assert 'reporter' in result
            task = task_control.tasks[result['name']]
            getattr(self.reporter, result['reporter'])(task)


    def execute_task(self, task_q, result_q):
        """executed on child processes"""
        self.result_q = result_q
        self.reporter = self.MP_Reporter(self, self.reporter)

        try:
            while True:
                task = task_q.get()
                if task is None:
                    self.teardown()
                    return # no more tasks to execute finish this process

                if isinstance(task, Hold):
                    continue

                result = {'name': task.name}
                # FIXME support setup objects with 2 "scopes" (global and process)
                if task.setup:
                    raise Exception("Task '%s' has Setup-objects. " % task.name +
                                    "Setup-objects are deprecated and not" +
                                    " supported with Multi-processing.")

                t_result = Runner.execute_task(self, task)

                if t_result is None:
                    result['result'] = task.result
                    result['values'] = task.values
                else:
                    result['failure'] = t_result

                result_q.put(result)
        except (SystemExit, KeyboardInterrupt, Exception), e:
            # error, blow-up everything
            result_q.put({'name': task.name,
                          'exit': e.__class__,
                          'exception': str(e)})