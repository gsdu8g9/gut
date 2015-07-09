import os
import random
import sys
import time

import plumbum

import config
import deps
from terminal import out, out_dim, dim, pipe_quote, color_host_path, kill_previous_process, save_process_pid, get_pidfile_path, active_pidfiles, shutting_down, shutdown, run_daemon_thread, get_cmd

def rsync(src_context, src_path, dest_context, dest_path, excludes=[]):
    def get_path_str(context, path):
        return '%s%s%s/' % (context._ssh_address, ':' if context._ssh_address else '', context.path(path),)
    src_path_str = get_path_str(src_context, src_path)
    dest_path_str = get_path_str(dest_context, dest_path)
    out(dim('Uploading ') + color_host_path(src_context, src_path) + dim(' to ') + color_host_path(dest_context, dest_path) + dim('...'))
    mkdirp(dest_context, dest_path)
    if src_context._is_windows:
        root_path = os.path.normpath(os.path.expanduser(unicode(src_path)))
        for root, folders, files in os.walk(root_path):
            dest_folder = dest_context.path(dest_path) / os.path.relpath(root, root_path).replace('\\', '/')
            mkdirp(dest_context, dest_folder)
            for filename in files:
                if filename not in excludes:
                    abs_path = os.path.join(root, filename)
                    rel_path = os.path.relpath(abs_path, root_path)
                    remote_path = dest_context.path(dest_path) / rel_path.replace('\\', '/')
                    # out('Uploading ' + rel_path + ' to ' +  unicode(remote_path) + '...')
                    dest_context.upload(src_context.path(abs_path), remote_path)
                    if '.' not in filename  or filename.endswith('.sh'):
                        # out(' CHMOD +x %s' % (remote_path,))
                        dest_context['chmod']['+x', remote_path]()
                    # out(' done.\n')
            orig_folders = tuple(folders)
            del folders[:]
            for folder in orig_folders:
                if folder not in excludes:
                    folders.append(folder)
    else:
        rsync = plumbum.local['rsync']['-a']
        for exclude in excludes:
            rsync = rsync['--exclude=%s' % (exclude,)]
        rsync[src_path_str, dest_path_str]()
    out_dim(' done.\n')

INOTIFY_CHANGE_EVENTS = ['modify', 'attrib', 'move', 'create', 'delete']
def append_inotify_change_events(context, watcher):
    if context._is_windows:
        return watcher['--event', ','.join(INOTIFY_CHANGE_EVENTS)]
    for event in INOTIFY_CHANGE_EVENTS:
        watcher = watcher['--event', event]
    return watcher

def watch_for_changes(context, path, event_prefix, event_queue):
    proc = None
    with context.cwd(context.path(path)):
        watched_root = (context['cmd']['/c', 'cd ,']() if context._is_windows else context['pwd']()).strip()
        def run_watcher():
            watch_type = get_cmd(context, ['inotifywait', 'fswatch'])
            watcher = None
            if watch_type == 'inotifywait':
                # inotify-win has slightly different semantics (and a completely different regex engine) than inotify-tools
                format_str = '%w\%f' if context._is_windows else '%w%f'
                exclude_str = '\\.gut($|\\\\)' if context._is_windows else '\.gut/'
                watcher = context['inotifywait']['--quiet', '--monitor', '--recursive', '--format', format_str, '--exclude', exclude_str]
                watcher = append_inotify_change_events(context, watcher)
                watcher = watcher['./']
            elif watch_type == 'fswatch':
                watcher = context['fswatch']['./']
            else:
                raise Exception('missing ' + ('fswatch' if context._is_osx else 'inotifywait'))
            out(dim('Using ') + watch_type + dim(' to listen for changes in ') + context._sync_path + '\n')
            kill_previous_process(context, watch_type)
            proc = watcher.popen()
            save_process_pid(context, watch_type, proc.pid)
            return proc
        proc = deps.retry_method(context, run_watcher)
    def run():
        while not shutting_down():
            line = proc.stdout.readline()
            if line != '':
                changed_path = line.rstrip()
                changed_path = os.path.abspath(os.path.join(watched_root, changed_path))
                rel_path = os.path.relpath(changed_path, watched_root)
                # out('changed_path: ' + changed_path + '\n')
                # out('watched_root: ' + watched_root + '\n')
                # out('changed ' + changed_path + ' -> ' + rel_path + '\n')
                event_queue.put((event_prefix, rel_path))
            else:
                break
    run_daemon_thread(run)
    pipe_quote('watch_%s_err' % (event_prefix,), proc.stderr)

def start_ssh_tunnel(local, remote, gutd_bind_port, gutd_connect_port, autossh_monitor_port):
    cmd = get_cmd(local, ['autossh', 'ssh'])
    if not cmd:
        deps.missing_dependency(local, 'ssh')
    ssh_tunnel_opts = '%s:localhost:%s' % (gutd_connect_port, gutd_bind_port)
    kill_previous_process(local, cmd)
    command = local[cmd]
    if cmd == 'autossh' and local._is_osx:
        command = command['-M', autossh_monitor_port]
    command = command['-N', '-L', ssh_tunnel_opts, '-R', ssh_tunnel_opts, remote._ssh_address]
    proc = command.popen()
    save_process_pid(local, cmd, proc.pid)
    pipe_quote(local, cmd + '_out', proc.stdout)
    pipe_quote(local, cmd + '_err', proc.stderr)

def restart_on_change(exe_path):
    def run():
        local = plumbum.local
        watch_path = os.path.dirname(os.path.abspath(__file__))
        try:
            changed = append_inotify_change_events(local, local['inotifywait'])['--quiet', '--recursive', '--', local.path(watch_path)]() # blocks until there's a change
        except plumbum.commands.ProcessExecutionError as ex:
            out(dim('(dev-mode) inotifywait exited with [') + unicode(ex,) + dim(']\n'))
        else:
            out_dim('\n(dev-mode) Restarting due to [%s]...\n' % (changed.strip(),))
            while True:
                try:
                    os.execv(unicode(exe_path), sys.argv)
                except Exception as ex:
                    out('error restarting: %s\n' % (ex,))
                    time.sleep(1)
    run_daemon_thread(run)

def mkdirp(context, path):
    if context._is_windows:
        if context._is_local:
            _path = os.path.normpath(os.path.expanduser(unicode(path)))
            if not os.path.exists(_path):
                os.makedirs(_path)
        else:
            raise Exception('Remote Windows not supported')
    else:
        context['mkdir']['-p', context.path(path)]()

def get_num_cores(context):
    if context._is_windows:
        return context['wmic']['CPU', 'Get', 'NumberOfLogicalProcessors', '/Format:List']().strip().split('=')[-1]
    else:
        return context['getconf']['_NPROCESSORS_ONLN']().strip()

def find_open_ports(contexts, num_ports):
    if not num_ports:
        return []
    netstats = ' '.join([context['netstat']['-nal']() for context in contexts])
    ports = []
    random_ports = range(config.MIN_RANDOM_PORT, config.MAX_RANDOM_PORT + 1)
    random.shuffle(random_ports)
    for port in random_ports:
        if not unicode(port) in netstats:
            ports.append(port)
        if len(ports) == num_ports:
            return ports
    raise Exception('Not enough available ports found')
