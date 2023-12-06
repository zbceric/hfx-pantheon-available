import os
from os import path
import sys
import socket
import signal
import errno
import json
import yaml
import subprocess
from datetime import datetime

from .context import base_dir, src_dir
from .subprocess_wrappers import check_call, check_output, call


def get_open_port():
    sock = socket.socket(socket.AF_INET)                            # 使用 IPv4 进行通信
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)      # SO_REUSEADDR 表示允许重用地址

    sock.bind(('', 0))                  # '': 使用任意可用的本地地址; 0: 使用系统分配的任意可用端口
    port = sock.getsockname()[1]        # 从中提取出端口号
    sock.close()                        # 关闭 sock
    return str(port)                    # 只为获取可用端口


def make_sure_dir_exists(d):
    try:
        os.makedirs(d)
    except OSError as exception:
        if exception.errno != errno.EEXIST:
            raise


tmp_dir = path.join(base_dir, 'tmp')
make_sure_dir_exists(tmp_dir)

# 定义配置文件存放位置
conf_dir = path.join(src_dir,'experiments','config')            # ~/pantheon/src/experiments/config
make_sure_dir_exists(conf_dir)                                  # 确认路径存在

def parse_config():
    with open(path.join(src_dir, 'config.yml')) as config:
        return yaml.load(config,Loader=yaml.FullLoader)         # 加载 ~/pantheon/src/config.yml


def update_submodules():
    cmd = 'git submodule update --init --recursive'             # --init 初始化子模块 | --recursive 递归检查子模块是否包含子模块
    check_call(cmd, shell=True)                                 # 更新子模块


class TimeoutError(Exception):
    pass


def timeout_handler(signum, frame):
    raise TimeoutError()


def utc_time():
    return datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')


def kill_proc_group(proc, signum=signal.SIGTERM):
    if not proc:
        return

    try:
        sys.stderr.write('kill_proc_group: killed process group with pgid %s\n'
                         % os.getpgid(proc.pid))
        os.killpg(os.getpgid(proc.pid), signum)
    except OSError as exception:
        sys.stderr.write('kill_proc_group: %s\n' % exception)


def apply_patch(patch_name, repo_dir):
    patch = path.join(src_dir, 'wrappers', 'patches', patch_name)

    if call(['git', 'apply', patch], cwd=repo_dir) != 0:
        sys.stderr.write('patch apply failed but assuming things okay '
                         '(patch applied previously?)\n')


def load_test_metadata(metadata_path):
    with open(metadata_path) as metadata:
        return json.load(metadata)


def verify_schemes_with_meta(schemes, meta):
    schemes_config = parse_config()['schemes']

    all_schemes = meta['cc_schemes']
    if schemes is None:
        cc_schemes = all_schemes
    else:
        cc_schemes = schemes.split()

    for cc in cc_schemes:
        if cc not in all_schemes:
            sys.exit('%s is not a scheme included in '
                     'pantheon_metadata.json' % cc)
        if cc not in schemes_config:
            sys.exit('%s is not a scheme included in src/config.yml' % cc)

    return cc_schemes


def who_runs_first(cc):
    cc_src = path.join(src_dir, 'wrappers', cc + '.py')

    cmd = [cc_src, 'run_first']                                         # 调用 wrappers, 传入参数为 run_first
    run_first = check_output(cmd).strip().decode('utf-8')

    if run_first == 'receiver':
        run_second = 'sender'
    elif run_first == 'sender':
        run_second = 'receiver'
    else:
        sys.exit('Must specify "receiver" or "sender" runs first')

    return run_first, run_second


def parse_remote_path(remote_path, cc=None):                    # 解析远程的路径
    ret = {}                                                            # e.g. zhang@10.103.11.54:~/pantheon

    ret['host_addr'], ret['base_dir'] = remote_path.rsplit(':', 1)      # host_addr: zhang@10.103.11.54 和 base_dir: ~/pantheon [右侧分割, 最多分割成两部分]
    ret['src_dir'] = path.join(ret['base_dir'], 'src')                  # src_dir: ~/pantheon/src
    ret['tmp_dir'] = path.join(ret['base_dir'], 'tmp')                  # tmp_dir: ~/pantheon/tmp
    ret['ip'] = ret['host_addr'].split('@')[-1]                         # ip: 10.103.11.54
    ret['ssh_cmd'] = ['ssh', ret['host_addr']]                          # ssh_cmd: ['ssh', 'zhang@10.103.11.54']
    ret['tunnel_manager'] = path.join(
        ret['src_dir'], 'experiments', 'tunnel_manager.py')             # tunnel_manager: ~/pantheon/src/experiments

    if cc is not None:
        ret['cc_src'] = path.join(ret['src_dir'], 'wrappers', cc + '.py')       # cc_src: ~/pantheon/src/wrappers/cubic.py

    return ret


def query_clock_offset(ntp_addr, ssh_cmd):
    local_clock_offset = None
    remote_clock_offset = None

    ntp_cmds = {}
    ntpdate_cmd = ['ntpdate', '-t', '5', '-quv', ntp_addr]

    ntp_cmds['local'] = ntpdate_cmd
    ntp_cmds['remote'] = ssh_cmd + ntpdate_cmd

    for side in ['local', 'remote']:
        cmd = ntp_cmds[side]

        fail = True
        for _ in range(3):
            try:
                offset = check_output(cmd)
                sys.stderr.write(offset)

                offset = offset.rsplit(' ', 2)[-2]
                offset = str(float(offset) * 1000)
            except subprocess.CalledProcessError:
                sys.stderr.write('Failed to get clock offset\n')
            except ValueError:
                sys.stderr.write('Cannot convert clock offset to float\n')
            else:
                if side == 'local':
                    local_clock_offset = offset
                else:
                    remote_clock_offset = offset

                fail = False
                break

        if fail:
            sys.stderr.write('Failed after 3 queries to NTP server\n')

    return local_clock_offset, remote_clock_offset


def get_git_summary(mode='local', remote_path=None):                # 默认测试 local
    git_summary_src = path.join(src_dir, 'experiments',
                                'git_summary.sh')
    local_git_summary = check_output(git_summary_src, cwd=base_dir)         # 调用 src/experiments/git_summary.sh, 打印子模块信息

    if mode == 'remote':                                                    # 远程测试, 需要建立真实连接
        r = parse_remote_path(remote_path)                                  # 解析路径

        git_summary_src = path.join(
            r['src_dir'], 'experiments', 'git_summary.sh')                  # ~/pantheon/src/experiments/git_summary.sh, 打印模块信息
        ssh_cmd = 'cd %s; %s' % (r['base_dir'], git_summary_src)            # cd ~/pantheon; ~/pantheon/src/experiments/git_summary.sh
        ssh_cmd = ' '.join(r['ssh_cmd']) + ' "%s"' % ssh_cmd                # ssh zhang@10.103.11.51 "cd ~/pantheon; ~/pantheon/src/experiments/git_summary.sh"

        remote_git_summary = check_output(ssh_cmd, shell=True)              # 执行命令, 指定 shell 运行, 获取远程子模块信息

        if local_git_summary != remote_git_summary:                         # 对比远程子模块信息和本地子模块信息
            sys.stderr.write(                                               # 不一致则打印信息
                '--- local git summary ---\n%s\n' % local_git_summary)
            sys.stderr.write(
                '--- remote git summary ---\n%s\n' % remote_git_summary)
            sys.exit('Repository differed between local and remote sides')  # 退出

    return local_git_summary

def decode_dict(d):
    result = {}
    for key, value in d.items():
        if isinstance(key, bytes):
            key = key.decode()
        if isinstance(value, bytes):
            value = value.decode()
        elif isinstance(value, dict):
            value = decode_dict(value)
        result.update({key: value})
    return result

def save_test_metadata(meta, metadata_path):
    meta.pop('all')
    meta.pop('schemes')
    meta.pop('data_dir')
    meta.pop('pkill_cleanup')               # 从字典中去掉这几个索引

    # use list in case meta.keys() returns an iterator in Python 3
    for key in list(meta.keys()):           # 遍历 meta 的 key
        if meta[key] is None:               # 去掉空的 key
            meta.pop(key)

    if 'uplink_trace' in meta:
        meta['uplink_trace'] = path.basename(meta['uplink_trace'])              # 默认 'src/experiments/12mbps.trace'
    if 'downlink_trace' in meta:
        meta['downlink_trace'] = path.basename(meta['downlink_trace'])          # 默认 'src/experiments/12mbps.trace'

    with open(metadata_path, 'w') as metadata_fh:
        json.dump(decode_dict(meta), metadata_fh, sort_keys=True, indent=4,
                  separators=(',', ': '))


def get_sys_info():
    sys_info = ''
    sys_info += check_output(['uname', '-sr']).decode()
    sys_info += check_output(['sysctl', 'net.core.default_qdisc']).decode()
    sys_info += check_output(['sysctl', 'net.core.rmem_default']).decode()
    sys_info += check_output(['sysctl', 'net.core.rmem_max']).decode()
    sys_info += check_output(['sysctl', 'net.core.wmem_default']).decode()
    sys_info += check_output(['sysctl', 'net.core.wmem_max']).decode()
    sys_info += check_output(['sysctl', 'net.ipv4.tcp_rmem']).decode()
    sys_info += check_output(['sysctl', 'net.ipv4.tcp_wmem']).decode()
    return sys_info
