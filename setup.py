import os
import argparse
import logging
import sys
import traceback
import shlex
from collections import OrderedDict
from multiprocessing import cpu_count
from .util import FatalError, Namespace, qjoin


# disable .pyc file generation
sys.dont_write_bytecode = True


class Setup:
    def __init__(self, root_path):
        self.root_path = os.path.abspath(root_path)
        self.instances = OrderedDict()
        self.targets = OrderedDict()

    def main(self):
        self.ctx = Namespace()
        self.init_context()
        self.parse_argv()
        self.initialize_logger()
        self.create_dirs()
        self.run_command()

    def parse_argv(self):
        parser = argparse.ArgumentParser(
                description='Frontend for building/running instrumented benchmarks.')

        nproc = cpu_count()

        # global options
        parser.add_argument('-v', '--verbosity', default='info',
                choices=['critical', 'error', 'warning', 'info', 'debug'],
                help='set logging verbosity (default info)')

        self.subparsers = parser.add_subparsers(
                title='subcommands', metavar='command', dest='command',
                description='run with "<command> --help" to see options for '
                            'individual commands')
        self.subparsers.required = True

        # command: build
        pbuild = self.subparsers.add_parser('build',
                help='build target programs (also builds dependencies)')
        pbuild.add_argument('-t', '--targets', nargs='+', metavar='TARGET',
                default=[], choices=self.targets,
                help='which target programs to build')
        pbuild.add_argument('-i', '--instances', nargs='+', metavar='INSTANCE',
                default=[], choices=self.instances,
                help='which instances to build')
        pbuild.add_argument('-p', '--packages', nargs='+', metavar='PACKAGE',
                default=[],
                help='which packages to build (either on top of dependencies, '
                     'or to force a rebuild)').completer = self.complete_pkg
        pbuild.add_argument('-j', '--jobs', type=int, default=nproc,
                help='maximum number of build processes (default %d)' % nproc)
        pbuild.add_argument('--deps-only', action='store_true',
                help='only build dependencies, not targets themselves')
        pbuild.add_argument('--force-rebuild-deps', action='store_true',
                help='always run the build commands')
        pbuild.add_argument('--relink', action='store_true',
                help='only link targets, don\'t rebuild object files')
        pbuild.add_argument('--clean', action='store_true',
                help='clean targets and packages (not all deps, only from -p) first')
        pbuild.add_argument('-n', '--dry-run', action='store_true',
                help='don\'t actually build anything, just show what will be done')

        for target in self.targets.values():
            target.add_build_args(pbuild)

        for instance in self.instances.values():
            instance.add_build_args(pbuild)

        # command: clean
        pclean = self.subparsers.add_parser('clean',
                help='remove all source/build/install files of the given '
                     'packages and targets')
        pclean.add_argument('-t', '--targets', nargs='+', metavar='TARGET',
                default=[], choices=self.targets,
                help='which target programs to clean')
        pclean.add_argument('-p', '--packages', nargs='+', metavar='PACKAGE',
                default=[],
                help='which packages to clean').completer = self.complete_pkg

        # command: run
        prun = self.subparsers.add_parser('run',
                help='run target program (does not build anything)')
        prun.add_argument('target', choices=self.targets,
                help='which target to run')
        prun.add_argument('instance', choices=self.instances,
                help='which instance to run')

        # command: config
        pconfig = self.subparsers.add_parser('config',
                help='print information about command line arguments and build flags')
        pconfig_group = pconfig.add_mutually_exclusive_group(required=True)
        pconfig_group.add_argument('--instances', action='store_true',
                dest='list_instances',
                help='list all registered instances')
        pconfig_group.add_argument('--targets', action='store_true',
                dest='list_targets',
                help='list all registered targets')
        pconfig_group.add_argument('--packages', action='store_true',
                dest='list_packages',
                help='list dependencies of all registered targets/instances')

        # command: pkg-config
        ppkgconfig = self.subparsers.add_parser('pkg-config',
                help='print package-specific information')
        ppkgconfig.add_argument('package',
                help='package to configure').completer = self.complete_pkg
        #ppkgconfig.add_argument('option',
        #        help='configuration option').completer = self.complete_pkg_config
        ppkgconfig.add_argument('args', nargs=argparse.REMAINDER, choices=[],
                help='configuration args')

        # enable bash autocompletion if supported
        try:
            import argcomplete
            argcomplete.autocomplete(parser, always_complete_options=False)
        except ImportError:
            pass

        self.args = parser.parse_args()
        if 'jobs' in self.args:
            self.ctx.jobs = self.args.jobs

    def complete_pkg(self, prefix, parsed_args, **kwargs):
        objs = list(self.targets.values())
        objs += list(self.instances.values())
        for package in self.get_deps(objs):
            name = package.ident()
            if name.startswith(prefix):
                yield name

    #def complete_pkg_config(self, prefix, parsed_args, **kwargs):
    #    package = self.get_package(parsed_args.package)
    #    return (arg for arg, desc, value in package.pkg_config(self.ctx)
    #            if arg.startswith(prefix))

    def init_context(self):
        self.ctx.hooks = Namespace(post_build=[])

        self.ctx.paths = paths = Namespace()
        paths.root = self.root_path
        paths.infra = os.path.dirname(__file__)
        paths.buildroot = os.path.join(paths.root, 'build')
        paths.log = os.path.join(paths.buildroot, 'log')
        paths.debuglog = os.path.join(paths.log, 'debug.txt')
        paths.runlog = os.path.join(paths.log, 'commands.txt')
        paths.packages = os.path.join(paths.buildroot, 'packages')
        paths.targets = os.path.join(paths.buildroot, 'targets')

        # FIXME move to package?
        self.ctx.prefixes = []
        self.ctx.cc = 'cc'
        self.ctx.cxx = 'c++'
        self.ctx.ar = 'ar'
        self.ctx.nm = 'nm'
        self.ctx.ranlib = 'ranlib'
        self.ctx.cflags = []
        self.ctx.ldflags = []

    def create_dirs(self):
        os.makedirs(self.ctx.paths.log, exist_ok=True)
        os.makedirs(self.ctx.paths.packages, exist_ok=True)
        os.makedirs(self.ctx.paths.targets, exist_ok=True)

    def initialize_logger(self):
        fmt = '%(asctime)s [%(levelname)s] %(message)s'
        datefmt = '%H:%M:%S'

        self.ctx.log = log = logging.getLogger('autosetup')
        log.setLevel(logging.DEBUG)
        log.propagate = False

        termlog = logging.StreamHandler(sys.stdout)
        termlog.setLevel(getattr(logging, self.args.verbosity.upper()))
        termlog.setFormatter(logging.Formatter(fmt, datefmt))
        log.addHandler(termlog)

        # always write debug log to file
        debuglog_path = os.path.join(self.ctx.paths.log, 'debug.txt')
        debuglog = logging.FileHandler(debuglog_path, mode='w')
        debuglog.setLevel(logging.DEBUG)
        debuglog.setFormatter(logging.Formatter(fmt, '%Y-%m-%d ' + datefmt))
        log.addHandler(debuglog)

        # colorize log if supported
        try:
            import coloredlogs
            coloredlogs.install(logger=log, fmt=fmt, datefmt=datefmt,
                                level=termlog.level)
        except ImportError:
            pass

    def add_instance(self, instance):
        if instance.name in self.instances:
            self.ctx.log.warning('overwriting existing instance "%s"' % instance)
        self.instances[instance.name] = instance

    def get_instance(self, name):
        if name not in self.instances:
            raise FatalError('no instance called "%s"' % name)
        return self.instances[name]

    def add_target(self, target):
        if target.name in self.targets:
            self.ctx.log.warning('overwriting existing target "%s"' % target)
        self.targets[target.name] = target

    def get_target(self, name):
        if name not in self.targets:
            raise FatalError('no target called "%s"' % name)
        return self.targets[name]

    def get_package(self, name):
        objs = list(self.targets.values())
        objs += list(self.instances.values())
        for package in self.get_deps(objs):
            if package.ident() == name:
                return package
        raise FatalError('no package called %s' % name)

    def get_deps(self, objs):
        deps = []

        def add_dep(dep, visited):
            if dep in visited:
                raise FatalError('recursive dependency %s' % dep)
            visited.add(dep)

            for nested_dep in dep.dependencies():
                add_dep(nested_dep, set(visited))

            #if dep in deps:
            #    self.ctx.log.debug('skipping duplicate dependency %s' % dep.ident())
            #else:
            if dep not in deps:
                deps.append(dep)

        for obj in objs:
            for dep in obj.dependencies():
                add_dep(dep, set())

        return deps

    def fetch_package(self, package, force_rebuild, *args):
        package.goto_rootdir(self.ctx)

        if package.is_fetched(self.ctx):
            self.ctx.log.debug('%s already fetched, skip' % package.ident())
        elif not force_rebuild and package.is_installed(self.ctx):
            self.ctx.log.debug('%s already installed, skip fetching' % package.ident())
        else:
            self.ctx.log.info('fetching %s' % package.ident())
            if not self.args.dry_run:
                package.goto_rootdir(self.ctx)
                package.fetch(self.ctx, *args)

    def build_package(self, package, force_rebuild, *args):
        package.goto_rootdir(self.ctx)

        if not force_rebuild and package.is_built(self.ctx):
            self.ctx.log.debug('%s already built, skip' % package.ident())
            return
        elif not force_rebuild and package.is_installed(self.ctx):
            self.ctx.log.debug('%s already installed, skip building' % package.ident())
        else:
            self.ctx.log.info('building %s' % package.ident())
            if not self.args.dry_run:
                package.goto_rootdir(self.ctx)
                package.build(self.ctx, *args)

    def install_package(self, package, force_rebuild, *args):
        package.goto_rootdir(self.ctx)

        if not force_rebuild and package.is_installed(self.ctx):
            self.ctx.log.debug('%s already installed, skip' % package.ident())
        else:
            self.ctx.log.info('installing %s' % package.ident())
            if not self.args.dry_run:
                package.goto_rootdir(self.ctx)
                package.install(self.ctx, *args)

        package.goto_rootdir(self.ctx)

    def clean_package(self, package):
        if package.is_clean(self.ctx):
            self.ctx.log.debug('package %s is already cleaned' % package.ident())
        else:
            self.ctx.log.info('cleaning package ' + package.ident())
            if not self.args.dry_run:
                package.clean(self.ctx)

    def clean_target(self, target):
        if target.is_clean(self.ctx):
            self.ctx.log.debug('target %s is already cleaned' % target.name)
        else:
            self.ctx.log.info('cleaning target ' + target.name)
            if not self.args.dry_run:
                target.clean(self.ctx)

    def run_build(self):
        targets = [self.get_target(name) for name in self.args.targets]
        instances = [self.get_instance(name) for name in self.args.instances]
        packages = [self.get_package(name) for name in self.args.packages]

        if self.args.deps_only:
            if not targets and not instances and not packages:
                raise FatalError('no targets or instances specified')
        elif (not targets or not instances) and not packages:
            raise FatalError('need at least one target and instance to build')

        deps = self.get_deps(targets + instances + packages)
        force_deps = set()
        separate_packages = []
        for package in packages:
            if package in deps:
                force_deps.add(package)
            else:
                separate_packages.append(package)

        # clean packages and targets if requested
        if self.args.clean:
            for package in packages:
                self.clean_package(package)
            for target in targets:
                self.clean_target(target)

        # first fetch all necessary code so that the internet connection can be
        # broken during building
        for package in deps + separate_packages:
            self.fetch_package(package, self.args.force_rebuild_deps)

        if not self.args.deps_only:
            for target in targets:
                target.goto_rootdir(self.ctx)
                if target.is_fetched(self.ctx):
                    self.ctx.log.debug('%s already fetched, skip' % target.name)
                else:
                    self.ctx.log.info('fetching %s' % target.name)
                    target.fetch(self.ctx)

        cached_deps = {t: self.get_deps([t]) for t in targets}
        for i in instances:
            cached_deps[i] = self.get_deps([i])
        for p in separate_packages:
            cached_deps[p] = self.get_deps([p])

        built_packages = set()

        def build_package_once(package, force):
            if package not in built_packages:
                self.build_package(package, force)
                self.install_package(package, force)
                built_packages.add(package)

        def build_deps_once(obj):
            for package in cached_deps[obj]:
                force = self.args.force_rebuild_deps or package in force_deps
                build_package_once(package, force)
                package.install_env(self.ctx)

        for package in separate_packages:
            oldctx = self.ctx.copy()
            build_deps_once(package)
            build_package_once(package, True)
            self.ctx = oldctx

        for instance in instances:
            # use a copy of the context for instance configuration to avoid
            # stacking configurations between instances
            # FIXME: only copy the build env (the part that changes)
            oldctx = self.ctx.copy()
            instance.configure(self.ctx)
            build_deps_once(instance)

            for target in targets:
                oldctx = self.ctx.copy()
                build_deps_once(target)

                if not self.args.deps_only:
                    self.ctx.log.info('building %s-%s' % (target.name, instance.name))
                    if not self.args.dry_run:
                        if not self.args.relink:
                            target.goto_rootdir(self.ctx)
                            target.build(self.ctx, instance)
                        target.goto_rootdir(self.ctx)
                        target.link(self.ctx, instance)
                        target.run_hooks_post_build(self.ctx, instance)

                self.ctx = oldctx

            self.ctx = oldctx

    def run_clean(self):
        packages = [self.get_package(name) for name in self.args.packages]
        targets = [self.get_target(name) for name in self.args.targets]
        if not packages and not targets:
            raise FatalError('no packages or targets specified')

        self.args.dry_run = False
        for package in packages:
            self.clean_package(package)
        for target in targets:
            self.clean_target(target)

    def run_run(self):
        raise NotImplementedError

    def run_config(self):
        if self.args.list_instances:
            for name in self.instances.keys():
                print(name)
        elif self.args.list_targets:
            for name in self.targets.keys():
                print(name)
        elif self.args.list_packages:
            objs = list(self.targets.values())
            objs += list(self.instances.values())
            for package in self.get_deps(objs):
                print(package.ident())
        else:
            raise NotImplementedError

    def run_pkg_config(self):
        package = self.get_package(self.args.package)
        parser = self.subparsers.add_parser(
                '%s %s' % (self.args.command, package.ident()))
        pgroup = parser.add_mutually_exclusive_group(required=True)
        for opt, desc, value in package.pkg_config_options(self.ctx):
            pgroup.add_argument(opt, action='store_const', dest='value',
                                const=value, help=desc)
        value = parser.parse_args(self.args.args).value

        # for lists (handy for flags), join by spaces while adding quotes where
        # necessary
        if isinstance(value, (list, tuple)):
            value = qjoin(value)

        print(value)

    def run_command(self):
        try:
            os.chdir(self.ctx.paths.root)

            if self.args.command == 'build':
                self.run_build()
            elif self.args.command == 'clean':
                self.run_clean()
            elif self.args.command == 'run':
                self.run_run()
            elif self.args.command == 'config':
                self.run_config()
            elif self.args.command == 'pkg-config':
                self.run_pkg_config()
            else:
                raise FatalError('unknown command %s' % self.args.command)
        except FatalError as e:
            self.ctx.log.error(str(e))
        except KeyboardInterrupt:
            self.ctx.log.info('exiting because of keyboard interrupt')
        except Exception as e:
            self.ctx.log.critical('unkown error\n' + traceback.format_exc().rstrip())
