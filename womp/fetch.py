from __future__ import unicode_literals

from gevent import monkey
monkey.patch_all()

import os
import time
import json
import codecs
from argparse import ArgumentParser

from gevent import pool
from gevent.greenlet import Greenlet
from gevent.threadpool import ThreadPool

from wapiti import WapitiClient
from article_list import ArticleListManager
from dashboard import create_fetch_dashboard
from inputs import DEFAULT_INPUTS

DEFAULT_EXT = '.fetch_data'
DEFAULT_CONC = 20
DEFAULT_LIMITS = {
    # Backlinks: 100,
    # FeedbackV4: 100,
}


def rotated_sequence(seq, start_index):
    n = len(seq)
    for i in xrange(n):
        yield seq[(i + start_index) % n]


class FancyInputPool(pool.Pool):
    def __init__(self, limits, *args, **kwargs):
        self.limits = limits if limits is not None else {}
        self.pools = {}  # will be lazily initialized in add()
        super(FancyInputPool, self).__init__(*args, **kwargs)

    def add(self, grn, *args, **kwargs):
        super(FancyInputPool, self).add(grn)
        for in_type, limit in self.limits.iteritems():
            if isinstance(grn, in_type):
                fancy_pool = self.pools.get(in_type)
                if fancy_pool is None:
                    self.pools[in_type] = fancy_pool = pool.Pool(limit)
                fancy_pool.add(grn)
        return


class FetchManager(object):
    def __init__(self, env_or_path=None, debug=True):
        self.alm = ArticleListManager(env_or_path)
        if not env_or_path or isinstance(env_or_path, basestring):
            self.env = None
            defpath = env_or_path or os.getenv('WOMP_FETCH_HOME') \
                or os.getcwd()
            self._home = defpath
        else:
            self.env = env_or_path
            self._home = self.env.fetch_home
        self.debug = debug
        self.results = []
        self.result_stats = []
        self.concurrency = DEFAULT_CONC
        self.limits = DEFAULT_LIMITS
        self.inputs = DEFAULT_INPUTS
        self.input_pool = FancyInputPool(self.limits)
        self.pool = pool.Pool(self.concurrency)

    def load_list(self, name):
        self.name = name
        article_list = self.alm.load_list(name)
        self.articles = article_list.get_articles()

    def run_fetch(self):
        self.start_time = time.time()
        print 'Booting up wapiti...'
        self.wapiti_client = WapitiClient('makuro@makuro.org')  # todo: config
        if self.dashboard:
            self.spawn_dashboard()
        print 'Creating Loupes for', len(self.articles), 'articles...'
        for i, ai in enumerate(self.articles):
            ft = FetchTask(ai,
                           self.wapiti_client,
                           input_pool=self.input_pool,
                           input_classes=self.inputs,
                           order=i,
                           debug=self.debug)
            ft.link(self._on_fetch_task_complete)
            self.pool.start(ft)
        self.pool.join()

    def spawn_dashboard(self):
        print 'Spawning dashboard...'
        dashboard = create_fetch_dashboard(self)
        tpool = ThreadPool(2)
        tpool.spawn(dashboard.serve)

    def write(self):
        if not self.results:
            raise('no results!')
        print 'Writing...'
        print 'Total results:', len(self.results)
        print 'Incomplete results:', len([f for f in self.result_stats
                                          if not f['is_successful']])
        output_name = os.path.join(self._home, self.name + DEFAULT_EXT)
        output_file = codecs.open(output_name, 'w', 'utf-8')
        for result in self.results:
            output_file.write(json.dumps(result, default=str))
            output_file.write('\n')
        output_file.close()

    def _on_fetch_task_complete(self, fetch_task):
        results = fetch_task.results
        results['page_info'] = {'title': fetch_task.page_info.title,
                                'page_id': fetch_task.page_info.page_id,
                                'ns': fetch_task.page_info.ns,
                                'subject_id': fetch_task.page_info.subject_id,
                                'talk_id': fetch_task.page_info.talk_id}
        self.results.append(fetch_task.results)
        result_stats = fetch_task.get_status()
        result_stats['title'] = fetch_task.page_info.title
        self.result_stats.append(result_stats)
        if self.debug:
            print self._calculate_fetch_stats(fetch_task)

        #calculate fetch times
        # saving

    def _calculate_fetch_stats(self, fetch_task):
        msg_params = fetch_task.completion_stats
        msg_params['num'] = len(self.results)
        msg_params['total'] = len(self.articles)
        return u'#{num}/{total} (#{order}) "{title}" took {dur:.4f} seconds'\
               .format(**msg_params)


class FetchTask(Greenlet):
    def __init__(self, page_info, wapiti_client, *args, **kwargs):
        self.page_info = page_info
        # defaults
        input_classes = kwargs.pop('input_classes', DEFAULT_INPUTS)
        input_pool = kwargs.pop('input_pool', pool.Pool())
        self.order = kwargs.pop('order', 0)
        self.debug = kwargs.pop('debug', False)
        self.inputs = [i(self.page_info, wapiti_client, debug=self.debug)
                       for i in input_classes]
        self.input_pool = input_pool
        self._int_input_pool = pool.Pool()
        self.results = {}
        self.fetch_results = {}
        self.times = {'create': time.time()}
        self._comp_inputs_count = 0
        super(FetchTask, self).__init__(*args, **kwargs)

    def process_inputs(self):
        for i in rotated_sequence(self.inputs, self.order):
            # time individual inputs?
            i.link(self._comp_hook)
            self._int_input_pool.add(i)
            self.input_pool.start(i)
        self._int_input_pool.join()
        return self

    def _run(self):
        return self.process_inputs()

    def _comp_hook(self, grnlt, **kwargs):
        self._comp_inputs_count += 1
        if grnlt.results:
            # todo: better printing
            self.results.update(grnlt.results)
        if self.is_complete:
            self.times['complete'] = time.time()

    def get_status(self):
        input_statuses = dict([(i.class_name, i.status) for i in self.inputs])
        is_complete = all([i['is_complete']
                           for i in input_statuses.itervalues()])
        is_successful = all([i['is_successful']
                             for i in input_statuses.itervalues()])
        ret = {
            'durations': self.durations,
            'page_id': self.page_info.page_id,
            'title': self.page_info.title,
            'create_time': self.times['create'],
            'inputs': input_statuses,
            'is_complete': is_complete,
            'is_successful': is_successful,
            'order': getattr(self, 'order', 0)
        }
        return ret

    @property
    def is_complete(self):
        #return len(self.results) == sum([len(i.stats) for i in self.inputs])
        return len(self.inputs) == self._comp_inputs_count

    @property
    def completion_stats(self):
        return {'order': self.order,
                'title': self.page_info.title,
                'dur': self.times['complete'] - self.times['create']
                # todo: errors?
                # todo: successful inputs?
                }

    @property
    def durations(self):
        ret = dict([(i.class_name, i.durations) for i in self.inputs])
        try:
            ret['total'] = self.times['complete'] - self.times['create']
        except KeyError:
            ret['total'] = time.time() - self.times['create']
        return ret


def add_subparsers(subparsers):
    parser_fetch = subparsers.add_parser('fetch')
    parser_fetch.add_argument('target_list_name', nargs='?',
                              help='Name of the list or list file')
    parser_fetch.add_argument('--save', help='save fetch results',
                              action='store_true')
    parser_fetch.add_argument('--no_pdb', help='end with pdb',
                              action='store_true')
    parser_fetch.add_argument('--no_dashboard', help='do not spawn dashboard',
                              action='store_true')
    parser_fetch.set_defaults(func=arg_fetch_list)
    return


def create_parser():
    """
    Only called when fetch is used directly (i.e., when there
    is no WompEnv).
    """
    root_parser = ArgumentParser(description='article fetch')
    root_parser.add_argument('--list_home', help='list lookup directory')
    add_subparsers(root_parser.add_subparsers())
    return root_parser


def arg_fetch_list(target_list_name,
                   list_home=None,
                   save=False,
                   no_pdb=False,
                   no_dashboard=False):
    use_dashboard = not no_dashboard
    fm = FetchManager(list_home)
    fm.load_list(target_list_name)
    fm.run(use_dashboard=use_dashboard)
    if save:
        fm.write()
    if not no_pdb:  # double negative for easier cli
        import pdb; pdb.set_trace()


def main():
    parser = create_parser()
    try:
        args = parser.parse_args()
    except SystemExit:
        parser.print_help()
        print
    kwargs = dict(args._get_kwargs())
    func = kwargs.pop('func')
    func(**kwargs)


def _main():
    fm = FetchManager()
    fm.load_list('test_coffee')
    test_page = [f for f in fm.articles][0]
    fm.run()
    import pdb; pdb.set_trace()


if __name__ == '__main__':
    main()
