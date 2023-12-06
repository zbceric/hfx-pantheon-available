#!/usr/bin/env python

from ast import arg
import os
from os import path
import sys
import time
import uuid
import random
import signal
import traceback
from subprocess import PIPE
from collections import namedtuple

import arg_parser
import context
from helpers import utils, kernel_ctl
from helpers.subprocess_wrappers import Popen, call


Flow = namedtuple('Flow', ['cc',                # replace self.cc
                           'cc_src_local',      # replace self.cc_src
                           'cc_src_remote',     # replace self.r[cc_src]
                           'run_first',         # replace self.run_first
                           'run_second'])       # replace self.run_second


class Test(object):
    def __init__(self, args, run_id, cc):
    
        self.mode = args.mode                                       # 'local' or 'remote'
        self.run_id = run_id
        self.cc = cc
        self.data_dir = path.abspath(args.data_dir)                 # src/experiments/data

        # shared arguments between local and remote modes
        self.flows = args.flows                                     # default: 1
        self.runtime = args.runtime                                 # default: 30
        self.interval = args.interval                               # default: 0
        self.run_times = args.run_times                             # default: 1

        # used for cleanup
        self.proc_first = None
        self.proc_second = None
        self.ts_manager = None
        self.tc_manager = None

        self.test_start_time = None
        self.test_end_time = None

        # local mode
        if self.mode == 'local':
            self.datalink_trace = args.uplink_trace                 # src/experiments/12mbps.trace
            self.acklink_trace = args.downlink_trace                # src/experiments/12mbps.trace
            self.prepend_mm_cmds = args.prepend_mm_cmds             # prepend mm-link
            self.append_mm_cmds = args.append_mm_cmds               # prepend mm-link --uplink-log=... --downlink-log=... append
            self.extra_mm_link_args = args.extra_mm_link_args       # prepend mm-link --uplink-log=... --downlink-log=... append extra

            # for convenience
            self.sender_side = 'remote'
            self.server_side = 'local'

        # remote mode
        if self.mode == 'remote':
            self.sender_side = args.sender_side
            self.server_side = args.server_side
            self.local_addr = args.local_addr
            self.local_if = args.local_if
            self.remote_if = args.remote_if
            self.local_desc = args.local_desc
            self.remote_desc = args.remote_desc

            self.ntp_addr = args.ntp_addr
            self.local_ofst = None
            self.remote_ofst = None

            self.r = utils.parse_remote_path(args.remote_path, self.cc)

        # arguments when there's a config
        self.test_config = None
        if hasattr(args, 'test_config'):
            self.test_config = args.test_config         # 加载 test_config

        if self.test_config is not None:                # 如果 test_config 有效
            self.cc = self.test_config['test-name']     # 从中查找测试算法, (如果没有配置 test_config, pantheon 通过命令行获取 cc, 否则通过 test_config 获取参数)
            self.flow_objs = {}
            cc_src_remote_dir = ''
            if self.mode == 'remote':
                cc_src_remote_dir = ['base_dir']

            tun_id = 1                                  # 隧道 id
            for flow in args.test_config['flows']:
                cc = flow['scheme']
                run_first, run_second = utils.who_runs_first(cc)

                local_p = path.join(context.src_dir, 'wrappers', cc + '.py')
                remote_p = path.join(cc_src_remote_dir, 'wrappers', cc + '.py')

                self.flow_objs[tun_id] = Flow(
                    cc=cc,
                    cc_src_local=local_p,
                    cc_src_remote=remote_p,
                    run_first=run_first,
                    run_second=run_second)
                tun_id += 1

    def setup_mm_cmd(self):
        mm_datalink_log = self.cc + '_mm_datalink_run%d.log' % self.run_id
        mm_acklink_log = self.cc + '_mm_acklink_run%d.log' % self.run_id
        self.mm_datalink_log = path.join(self.data_dir, mm_datalink_log)
        self.mm_acklink_log = path.join(self.data_dir, mm_acklink_log)

        if self.run_first == 'receiver' or self.flows > 0:
            # if receiver runs first OR if test inside pantheon tunnel
            uplink_log = self.mm_datalink_log
            downlink_log = self.mm_acklink_log
            uplink_trace = self.datalink_trace
            downlink_trace = self.acklink_trace
        else:
            # if sender runs first AND test without pantheon tunnel
            uplink_log = self.mm_acklink_log
            downlink_log = self.mm_datalink_log
            uplink_trace = self.acklink_trace
            downlink_trace = self.datalink_trace

        self.mm_cmd = []

        if self.prepend_mm_cmds:
            self.mm_cmd += self.prepend_mm_cmds.split()

        self.mm_cmd += [
            'mm-link', uplink_trace, downlink_trace,
            '--uplink-log=' + uplink_log,
            '--downlink-log=' + downlink_log]

        if self.extra_mm_link_args:
            self.mm_cmd += self.extra_mm_link_args.split()

        if self.append_mm_cmds:
            self.mm_cmd += self.append_mm_cmds.split()

    def prepare_tunnel_log_paths(self):
        # boring work making sure logs have correct paths on local and remote
        self.datalink_ingress_logs = {}
        self.datalink_egress_logs = {}
        self.acklink_ingress_logs = {}
        self.acklink_egress_logs = {}

        local_tmp = utils.tmp_dir

        if self.mode == 'remote':
            remote_tmp = self.r['tmp_dir']

        # tunnel log的名称设置
        for tun_id in range(1, self.flows + 1):
            uid = uuid.uuid4()

            datalink_ingress_logname = ('%s_flow%s_uid%s.log.ingress' %
                                        (self.datalink_name, tun_id, uid))
            self.datalink_ingress_logs[tun_id] = path.join(
                    local_tmp, datalink_ingress_logname)

            datalink_egress_logname = ('%s_flow%s_uid%s.log.egress' %
                                       (self.datalink_name, tun_id, uid))
            self.datalink_egress_logs[tun_id] = path.join(
                    local_tmp, datalink_egress_logname)

            acklink_ingress_logname = ('%s_flow%s_uid%s.log.ingress' %
                                       (self.acklink_name, tun_id, uid))
            self.acklink_ingress_logs[tun_id] = path.join(
                    local_tmp, acklink_ingress_logname)

            acklink_egress_logname = ('%s_flow%s_uid%s.log.egress' %
                                      (self.acklink_name, tun_id, uid))
            self.acklink_egress_logs[tun_id] = path.join(
                    local_tmp, acklink_egress_logname)

            if self.mode == 'remote':
                if self.sender_side == 'local':
                    self.datalink_ingress_logs[tun_id] = path.join(
                            remote_tmp, datalink_ingress_logname)
                    self.acklink_egress_logs[tun_id] = path.join(
                            remote_tmp, acklink_egress_logname)
                else:
                    self.datalink_egress_logs[tun_id] = path.join(
                            remote_tmp, datalink_egress_logname)
                    self.acklink_ingress_logs[tun_id] = path.join(
                            remote_tmp, acklink_ingress_logname)

    def setup(self):
        # setup commonly used paths
        self.cc_src = path.join(context.src_dir, 'wrappers', self.cc + '.py')       # "src/wrappers/bbr.py"
        self.tunnel_manager = path.join(context.src_dir, 'experiments',
                                        'tunnel_manager.py')                        # "src/experiments/tunnel_manager.py"

        # record who runs first
        if self.test_config is None:
            self.run_first, self.run_second = utils.who_runs_first(self.cc)         # first: receiver, second: sender
        else:
            self.run_first = None
            self.run_second = None

        # wait for 3 seconds until run_first is ready                                         
        self.run_first_setup_time = 3

        # setup output logs
        self.datalink_name = self.cc + '_datalink_run%d' % self.run_id              # 'bbr_datalink_run1'
        self.acklink_name = self.cc + '_acklink_run%d' % self.run_id                # 'bbr_acklink_run1'

        self.datalink_log = path.join(
            self.data_dir, self.datalink_name + '.log')             # '/src/experiments/data/bbr_datalink_run1.log'
        self.acklink_log = path.join(
            self.data_dir, self.acklink_name + '.log')              # '/src/experiments/data/bbr_acklink_run1.log'

        if self.flows > 0:
            self.prepare_tunnel_log_paths()                         # 设置 tunnel log 打印路径               

        if self.mode == 'local':
            self.setup_mm_cmd()                                     
            # 0: 'mm-link'
            # 1: '/home/zhangbochun/pantheon-3/src/experiments/12mbps.trace'
            # 2: '/home/zhangbochun/pantheon-3/src/experiments/12mbps.trace'
            # 3: '--uplink-log=/home/zhangbochun/pantheon-3/src/experiments/data/bbr_mm_datalink_run1.log'
            # 4: '--downlink-log=/home/zhangbochun/pantheon-3/src/experiments/data/bbr_mm_acklink_run1.log'
        else:
            # record local and remote clock offset
            if self.ntp_addr is not None:
                self.local_ofst, self.remote_ofst = utils.query_clock_offset(
                    self.ntp_addr, self.r['ssh_cmd'])

    # test congestion control without running pantheon tunnel
    def run_without_tunnel(self):
        port = utils.get_open_port()                                # 获取可用端口

        # run the side specified by self.run_first
        cmd = ['python', self.cc_src, self.run_first, port]
        sys.stderr.write('Running %s %s...\n' % (self.cc, self.run_first))
        self.proc_first = Popen(cmd, preexec_fn=os.setsid)

        # sleep just in case the process isn't quite listening yet
        # the cleaner approach might be to try to verify the socket is open
        time.sleep(self.run_first_setup_time)

        self.test_start_time = utils.utc_time()
        # run the other side specified by self.run_second
        sh_cmd = 'python %s %s $MAHIMAHI_BASE %s' % (
            self.cc_src, self.run_second, port)
        sh_cmd = ' '.join(self.mm_cmd) + " -- sh -c '%s'" % sh_cmd
        sys.stderr.write('Running %s %s...\n' % (self.cc, self.run_second))
        self.proc_second = Popen(sh_cmd, shell=True, preexec_fn=os.setsid)

        signal.signal(signal.SIGALRM, utils.timeout_handler)
        signal.alarm(self.runtime)

        try:
            self.proc_first.wait()
            self.proc_second.wait()
        except utils.TimeoutError:
            pass
        else:
            signal.alarm(0)
            sys.stderr.write('Warning: test exited before time limit\n')
        finally:
            self.test_end_time = utils.utc_time()

        return True

    def run_tunnel_managers(self):
        # run tunnel server manager
        if self.mode == 'remote':                                       # 采用 remote 模式测试
            if self.server_side == 'local':                             # server 在 local
                ts_manager_cmd = ['python', self.tunnel_manager]        # 使用本地的 tunnel_manager
            else:
                ts_manager_cmd = self.r['ssh_cmd'] + [
                    'python', self.r['tunnel_manager']]                 # server 在 remote, 使用 ssh 连接远程的 tunnel_manager
        else:
            ts_manager_cmd = ['python', self.tunnel_manager]            # 采用 local 模式测试, 使用本地的 tunnel_manager
            # 0: 'python'
            # 1: '/home/zhangbochun/pantheon-3/src/experiments/tunnel_manager.py'

        sys.stderr.write('[tunnel server manager (tsm)] ')              # 先启用 server
        self.ts_manager = Popen(ts_manager_cmd, stdin=PIPE, stdout=PIPE,
                                preexec_fn=os.setsid)       # 调用 tunnel_manager.py, 创建管道, 输入输出重定向到管道, os.setsid 用于在后台运行进程
        ts_manager = self.ts_manager

        while True:
            running = ts_manager.stdout.readline().decode('utf-8')
            if 'tunnel manager is running' in running:          # 等到 ts_manager (server) 运行起来再退出循环
                sys.stderr.write(running)
                break

        ts_manager.stdin.write(b'prompt [tsm]\n')               # 写入 prompt [tsm]\n (作为 server)
        ts_manager.stdin.flush()

        # run tunnel client manager
        if self.mode == 'remote':
            if self.server_side == 'local':
                tc_manager_cmd = self.r['ssh_cmd'] + [
                    'python', self.r['tunnel_manager']]
            else:
                tc_manager_cmd = ['python', self.tunnel_manager]
        else:
            tc_manager_cmd = self.mm_cmd + ['python', self.tunnel_manager]
            # local mode:
            # 0: 'mm-link'
            # 1: '/home/zhangbochun/pantheon-3/src/experiments/12mbps.trace'
            # 2: '/home/zhangbochun/pantheon-3/src/experiments/12mbps.trace'
            # 3: '--uplink-log=/home/zhangbochun/pantheon-3/src/experiments/data/bbr_mm_datalink_run1.log'
            # 4: '--downlink-log=/home/zhangbochun/pantheon-3/src/experiments/data/bbr_mm_acklink_run1.log'
            # 5: 'python'
            # 6: '/home/zhangbochun/pantheon-3/src/experiments/tunnel_manager.py'

        sys.stderr.write('[tunnel client manager (tcm)] ')
        self.tc_manager = Popen(tc_manager_cmd, stdin=PIPE, stdout=PIPE,
                                preexec_fn=os.setsid)           # 启动 Mahimahi 生成一个 link, 运行 tc_manager 作为 client
        tc_manager = self.tc_manager

        while True:
            running = tc_manager.stdout.readline().decode('utf-8')
            if 'tunnel manager is running' in running:
                sys.stderr.write(running)                       # 读取到指令, 说明 tc_manager 已经开始运行
                break

        tc_manager.stdin.write(b'prompt [tcm]\n')               # 向manager输入 'prompt [tcm]\n' (作为 client)
        tc_manager.stdin.flush()                                # flush

        return ts_manager, tc_manager

    def run_tunnel_server(self, tun_id, ts_manager):                    # 启动 server
        if self.server_side == self.sender_side:
            ts_cmd = 'mm-tunnelserver --ingress-log=%s --egress-log=%s' % (
                self.acklink_ingress_logs[tun_id],
                self.datalink_egress_logs[tun_id])
        else:
            # server 是接收端       server_side 'local', sender_side 'remote'
            ts_cmd = 'mm-tunnelserver --ingress-log=%s --egress-log=%s' % (
                self.datalink_ingress_logs[tun_id],
                self.acklink_egress_logs[tun_id])       # 'mm-tunnelserver --ingress-log=/home/zhangbochun/pantheon-3/tmp/bbr_datalink_run1_flow1_uid8c558a9c-fa2a-4c34-8861-20f090dfaf25.log.ingress --egress-log=/home/zhangbochun/pantheon-3/tmp/bbr_acklink_run1_flow1_uid8c558a9c-fa2a-4c34-8861-20f090dfaf25.log.egress'

        if self.mode == 'remote':
            if self.server_side == 'remote':
                if self.remote_if is not None:
                    ts_cmd += ' --interface=' + self.remote_if
            else:
                if self.local_if is not None:
                    ts_cmd += ' --interface=' + self.local_if

        # ts_cmd 格式为tunnel 1 mm-tunnelserver ingress_log_file egress_log_file
        ts_cmd = 'tunnel %s %s\n' % (tun_id, ts_cmd)            # 'tunnel 1 mm-tunnelserver --ingress-log=/home/zhangbochun/pantheon-3/tmp/bbr_datalink_run1_flow1_uiddb796b9c-3bdf-43ce-a70a-9fcadbc366ff.log.ingress --egress-log=/home/zhangbochun/pantheon-3/tmp/bbr_acklink_run1_flow1_uiddb796b9c-3bdf-43ce-a70a-9fcadbc366ff.log.egress\n'
        ts_manager.stdin.write(ts_cmd.encode('utf-8'))          # 第一条指令 tunnel 1 mm-tunnelserver, manager 启动 server
        ts_manager.stdin.flush()                                # 发送给 tsm server

        # read the command to run tunnel client
        readline_cmd = 'tunnel %s readline\n' % tun_id          # 第二条指令 tunnel 1 readline, 从中读取一条信息
        ts_manager.stdin.write(readline_cmd.encode('utf-8'))
        ts_manager.stdin.flush()

        cmd_to_run_tc = ts_manager.stdout.readline().decode('utf-8').split()        # 由 mm-tunnelserver 输出一条启动 mm-tunnelclient 的指令
        # 0: 'mm-tunnelclient'
        # 1: 'localhost'
        # 2: '45491' 
        # 3: '100.64.0.6' 
        # 4: '100.64.0.5'
        return cmd_to_run_tc             # 运行 client 的指令

    def run_tunnel_client(self, tun_id, tc_manager, cmd_to_run_tc):
        if self.mode == 'local':
            cmd_to_run_tc[1] = '$MAHIMAHI_BASE'             # local mode, 将 localhost 替换为 $MAHIMAHI_BASE
        else:
            if self.server_side == 'remote':
                cmd_to_run_tc[1] = self.r['ip']
            else:
                cmd_to_run_tc[1] = self.local_addr

        cmd_to_run_tc_str = ' '.join(cmd_to_run_tc)

        if self.server_side == self.sender_side:
            tc_cmd = '%s --ingress-log=%s --egress-log=%s' % (
                cmd_to_run_tc_str,
                self.datalink_ingress_logs[tun_id],
                self.acklink_egress_logs[tun_id])
        else:
            tc_cmd = '%s --ingress-log=%s --egress-log=%s' % (
                cmd_to_run_tc_str,
                self.acklink_ingress_logs[tun_id],
                self.datalink_egress_logs[tun_id])      # 'mm-tunnelclient $MAHIMAHI_BASE 51953 100.64.0.4 100.64.0.3 --ingress-log=tmp/bbr_acklink_run1_flow1_xxx.log.ingress --egress-log=tmp/bbr_datalink_run1_flow1_xxx.log.egress'

        if self.mode == 'remote':
            if self.server_side == 'remote':
                if self.local_if is not None:
                    tc_cmd += ' --interface=' + self.local_if
            else:
                if self.remote_if is not None:
                    tc_cmd += ' --interface=' + self.remote_if

        tc_cmd = 'tunnel %s %s\n' % (tun_id, tc_cmd)        # 指令前补充 tunnel id
        readline_cmd = 'tunnel %s readline\n' % tun_id      # 读取指令

        # re-run tunnel client after 20s timeout for at most 3 times
        max_run = 3
        curr_run = 0
        got_connection = ''
        while 'got connection' not in got_connection:
            curr_run += 1
            if curr_run > max_run:
                sys.stderr.write('Unable to establish tunnel\n')
                return False

            # 相同的步骤，先运行起来再readline
            tc_manager.stdin.write(tc_cmd.encode('utf-8'))              # 尝试运行 client mm-tunnelclient
            tc_manager.stdin.flush()
            while True:
                tc_manager.stdin.write(readline_cmd.encode('utf-8'))    # 不断读取
                tc_manager.stdin.flush()

                signal.signal(signal.SIGALRM, utils.timeout_handler)
                signal.alarm(20)

                try:
                    got_connection = tc_manager.stdout.readline().decode('utf-8')   # 从中 tc_manager 读取一行数据
                    sys.stderr.write('Tunnel is connected\n')
                except utils.TimeoutError:
                    sys.stderr.write('Tunnel connection timeout\n')     # 超时
                    break
                except IOError:
                    sys.stderr.write('Tunnel client failed to connect to '
                                     'tunnel server\n')
                    return False                                        # 没用建立连接
                else:
                    signal.alarm(0)
                    if 'got connection' in got_connection:
                        break

        return True

    def run_first_side(self, tun_id, send_manager, recv_manager,
                       send_pri_ip, recv_pri_ip):       # 参数: id, send 进程, recv 进程, send ip, recv ip

        first_src = self.cc_src             # '/home/zhangbochun/pantheon-3/src/wrappers/bbr.py'
        second_src = self.cc_src            # '/home/zhangbochun/pantheon-3/src/wrappers/bbr.py'、

        # 运行第一方并且返回第二方的cmd
        if self.run_first == 'receiver':                    # 如果先运行 receiver
            if self.mode == 'remote':
                if self.sender_side == 'local':
                    first_src = self.r['cc_src']
                else:
                    second_src = self.r['cc_src']

            port = utils.get_open_port()                    # 获取一个端口

            first_cmd = 'tunnel %s python %s receiver %s\n' % (
                tun_id, first_src, port)                            # cmd 1: 'tunnel 1 python src/wrappers/bbr.py receiver port\n'
            second_cmd = 'tunnel %s python %s sender %s %s\n' % (
                tun_id, second_src, recv_pri_ip, port)              # cmd 2: 'tunnel 1 python src/wrappers/bbr.py sender recv_ip port\n'

            recv_manager.stdin.write(first_cmd.encode('utf-8'))     # 将指令发送给 recv tunnel manage, 该指令被发送给 mm-tunnelserver, 调用 src/wrappers/bbr.py
            recv_manager.stdin.flush()                              # 每个算法都提供了启动流的方式, bbr 启动了 iperf server
        elif self.run_first == 'sender':  # self.run_first == 'sender'
            if self.mode == 'remote':
                if self.sender_side == 'local':
                    second_src = self.r['cc_src']
                else:
                    first_src = self.r['cc_src']

            port = utils.get_open_port()

            first_cmd = 'tunnel %s python %s sender %s\n' % (
                tun_id, first_src, port)
            second_cmd = 'tunnel %s python %s receiver %s %s\n' % (
                tun_id, second_src, send_pri_ip, port)

            send_manager.stdin.write(first_cmd.encode('utf-8'))
            send_manager.stdin.flush()

        # get run_first and run_second from the flow object
        else:
            assert(hasattr(self, 'flow_objs'))
            flow = self.flow_objs[tun_id]

            first_src = flow.cc_src_local
            second_src = flow.cc_src_local

            if flow.run_first == 'receiver':
                if self.mode == 'remote':
                    if self.sender_side == 'local':
                        first_src = flow.cc_src_remote
                    else:
                        second_src = flow.cc_src_remote

                port = utils.get_open_port()

                first_cmd = 'tunnel %s python %s receiver %s\n' % (
                    tun_id, first_src, port)
                second_cmd = 'tunnel %s python %s sender %s %s\n' % (
                    tun_id, second_src, recv_pri_ip, port)

                recv_manager.stdin.write(first_cmd.encode('utf-8'))
                recv_manager.stdin.flush()
            else:  # flow.run_first == 'sender'
                if self.mode == 'remote':
                    if self.sender_side == 'local':
                        second_src = flow.cc_src_remote
                    else:
                        first_src = flow.cc_src_remote

                port = utils.get_open_port()

                first_cmd = 'tunnel %s python %s sender %s\n' % (
                    tun_id, first_src, port)
                second_cmd = 'tunnel %s python %s receiver %s %s\n' % (
                    tun_id, second_src, send_pri_ip, port)

                send_manager.stdin.write(first_cmd.encode('utf-8'))
                send_manager.stdin.flush()

        return second_cmd           # 返回第二条指令, 对于 bbr local 测试来说, 就是启动 iperf -c xxx 的指令

    def run_second_side(self, send_manager, recv_manager, second_cmds):
        time.sleep(self.run_first_setup_time)
        self.run_first = 'receiver'                     # run_first: receiver
        start_time = time.time()                        # 当前时间
        self.test_start_time = utils.utc_time()         # 获取 utc 时间

        # start each flow self.interval seconds after the previous one
        for i in range(len(second_cmds)):               # 查看有几条指令需要启动
            if i != 0:
                time.sleep(self.interval)               # 间隔 interval 时间启动
            second_cmd = second_cmds[i]                 # 'tunnel 1 python /home/zhangbochun/pantheon-3/src/wrappers/bbr.py sender 100.64.0.3 40539\n'

            
            if self.run_first == 'receiver':
                send_manager.stdin.write(second_cmd.encode('utf-8'))        # 向 send_manager 发送指令, 启动 wrappers/bbr.py, 建立新的 iperf 流
                send_manager.stdin.flush()
            elif self.run_first == 'sender':
                recv_manager.stdin.write(second_cmd.encode('utf-8'))
                recv_manager.stdin.flush()
            else:
                assert(hasattr(self, 'flow_objs'))
                flow = self.flow_objs[i]
                if flow.run_first == 'receiver':
                    send_manager.stdin.write(second_cmd.encode('utf-8'))
                    send_manager.stdin.flush()
                elif flow.run_first == 'sender':
                    recv_manager.stdin.write(second_cmd.encode('utf-8'))
                    recv_manager.stdin.flush()

        elapsed_time = time.time() - start_time
        if elapsed_time > self.runtime:
            sys.stderr.write('Interval time between flows is too long')
            return False

        time.sleep(self.runtime - elapsed_time)
        self.test_end_time = utils.utc_time()

        return True

    # test congestion control using tunnel client and tunnel server
    def run_with_tunnel(self):                                  # 
        # run pantheon tunnel server and client managers
        ts_manager, tc_manager = self.run_tunnel_managers()

        # 如果sender_side是sever_side或者client_side 分别赋值
        # create alias for ts_manager and tc_manager using sender or receiver
        if self.sender_side == self.server_side:
            send_manager = ts_manager
            recv_manager = tc_manager
        else:
            send_manager = tc_manager                       # tunnel client - sender
            recv_manager = ts_manager                       # tunnel server - receiver

        # run every flow
        second_cmds = []
        for tun_id in range(1, self.flows + 1):                                     # 启动 flows 个流, id 从 1 开始
            # run tunnel server for tunnel tun_id
            cmd_to_run_tc = self.run_tunnel_server(tun_id, ts_manager)              # 启动 tunnel server

            # run tunnel client for tunnel tun_id
            if not self.run_tunnel_client(tun_id, tc_manager, cmd_to_run_tc):       # 建立 mm-client
                return False

            # 返回值 mm-tunnelclient localhost 49242 100.64.0.2 100.64.0.1          # mm-tunnelclient localhost port client_ip, server_ip
            tc_pri_ip = cmd_to_run_tc[3]  # tunnel client private IP    从指令中解析 ip
            ts_pri_ip = cmd_to_run_tc[4]  # tunnel server private IP

            if self.sender_side == self.server_side:
                send_pri_ip = ts_pri_ip     # send 获取 server ip
                recv_pri_ip = tc_pri_ip     # recv 获取 client ip
            else:                           # sender 'remote', server 'local'
                send_pri_ip = tc_pri_ip     # send 获取 client ip
                recv_pri_ip = ts_pri_ip     # recv 获取 server ip

            # run the side that runs first and get cmd to run the other side
            second_cmd = self.run_first_side(
                tun_id, send_manager, recv_manager, send_pri_ip, recv_pri_ip)       # 'tunnel 1 python src/wrappers/bbr.py sender 100.64.0.3 40539\n'
            second_cmds.append(second_cmd)              # 将指令存储在 second_cmds

        # run the side that runs second
        if not self.run_second_side(send_manager, recv_manager, second_cmds):       # 启动第二条指令 (client)
            return False

        # stop all the running flows and quit tunnel managers
        ts_manager.stdin.write(b'halt\n')               # 结束, 终止 ts_manager
        ts_manager.stdin.flush()
        tc_manager.stdin.write(b'halt\n')               # 结束, 终止 tc_manager
        tc_manager.stdin.flush()

        # process tunnel logs
        self.process_tunnel_logs()

        return True

    def download_tunnel_logs(self, tun_id):
        assert(self.mode == 'remote')

        # download logs from remote side
        cmd = 'scp -C %s:' % self.r['host_addr']
        cmd += '%(remote_log)s %(local_log)s'

        # function to get a corresponding local path from a remote path
        f = lambda p: path.join(utils.tmp_dir, path.basename(p))

        if self.sender_side == 'remote':
            local_log = f(self.datalink_egress_logs[tun_id])
            call(cmd % {'remote_log': self.datalink_egress_logs[tun_id],
                        'local_log': local_log}, shell=True)
            self.datalink_egress_logs[tun_id] = local_log

            local_log = f(self.acklink_ingress_logs[tun_id])
            call(cmd % {'remote_log': self.acklink_ingress_logs[tun_id],
                        'local_log': local_log}, shell=True)
            self.acklink_ingress_logs[tun_id] = local_log
        else:
            local_log = f(self.datalink_ingress_logs[tun_id])
            call(cmd % {'remote_log': self.datalink_ingress_logs[tun_id],
                        'local_log': local_log}, shell=True)
            self.datalink_ingress_logs[tun_id] = local_log

            local_log = f(self.acklink_egress_logs[tun_id])
            call(cmd % {'remote_log': self.acklink_egress_logs[tun_id],
                        'local_log': local_log}, shell=True)
            self.acklink_egress_logs[tun_id] = local_log


    def process_tunnel_logs(self):
        datalink_tun_logs = []
        acklink_tun_logs = []

        apply_ofst = False
        if self.mode == 'remote':
            if self.remote_ofst is not None and self.local_ofst is not None:
                apply_ofst = True

                if self.sender_side == 'remote':
                    data_e_ofst = self.remote_ofst
                    ack_i_ofst = self.remote_ofst
                    data_i_ofst = self.local_ofst
                    ack_e_ofst = self.local_ofst
                else:
                    data_i_ofst = self.remote_ofst
                    ack_e_ofst = self.remote_ofst
                    data_e_ofst = self.local_ofst
                    ack_i_ofst = self.local_ofst

        merge_tunnel_logs = path.join(context.src_dir, 'experiments',
                                      'merge_tunnel_logs.py')               # src/experiments/merge_tunnel_logs.py

        for tun_id in range(1, self.flows + 1):
            if self.mode == 'remote':
                self.download_tunnel_logs(tun_id)

            uid = uuid.uuid4()
            datalink_tun_log = path.join(
                utils.tmp_dir, '%s_flow%s_uid%s.log.merged'
                % (self.datalink_name, tun_id, uid))
            acklink_tun_log = path.join(
                utils.tmp_dir, '%s_flow%s_uid%s.log.merged'
                % (self.acklink_name, tun_id, uid))

            cmd = [merge_tunnel_logs, 'single',
                   '-i', self.datalink_ingress_logs[tun_id],
                   '-e', self.datalink_egress_logs[tun_id],
                   '-o', datalink_tun_log]
            # 0: '/home/zhangbochun/pantheon-3/src/experiments/merge_tunnel_logs.py'
            # 1: 'single'
            # 2: '-i'
            # 3: '/home/zhangbochun/pantheon-3/tmp/bbr_datalink_run1_flow1_uid11c75579-230b-4dbf-92fb-1f7c721017ba.log.ingress'
            # 4: '-e'
            # 5: '/home/zhangbochun/pantheon-3/tmp/bbr_datalink_run1_flow1_uid11c75579-230b-4dbf-92fb-1f7c721017ba.log.egress'
            # 6: '-o' 
            # 7: '/home/zhangbochun/pantheon-3/tmp/bbr_datalink_run1_flow1_uide3b84536-eafb-476c-a3f0-392db663c900.log.merged'
            if apply_ofst:
                cmd += ['-i-clock-offset', data_i_ofst,
                        '-e-clock-offset', data_e_ofst]
            call(cmd)

            cmd = [merge_tunnel_logs, 'single',
                   '-i', self.acklink_ingress_logs[tun_id],
                   '-e', self.acklink_egress_logs[tun_id],
                   '-o', acklink_tun_log]
            if apply_ofst:
                cmd += ['-i-clock-offset', ack_i_ofst,
                        '-e-clock-offset', ack_e_ofst]
            call(cmd)

            datalink_tun_logs.append(datalink_tun_log)
            acklink_tun_logs.append(acklink_tun_log)

        cmd = [merge_tunnel_logs, 'multiple', '-o', self.datalink_log]
        if self.mode == 'local':
            cmd += ['--link-log', self.mm_datalink_log]
        cmd += datalink_tun_logs
        call(cmd)

        cmd = [merge_tunnel_logs, 'multiple', '-o', self.acklink_log]
        if self.mode == 'local':
            cmd += ['--link-log', self.mm_acklink_log]
        cmd += acklink_tun_logs
        call(cmd)

    def run_congestion_control(self):
        if self.flows > 0:
            try:
                return self.run_with_tunnel()
            finally:
                utils.kill_proc_group(self.ts_manager)
                utils.kill_proc_group(self.tc_manager)
        else:
            # test without pantheon tunnel when self.flows = 0
            try:
                return self.run_without_tunnel()
            finally:
                utils.kill_proc_group(self.proc_first)
                utils.kill_proc_group(self.proc_second)

    def record_time_stats(self):
        stats_log = path.join(
            self.data_dir, '%s_stats_run%s.log' % (self.cc, self.run_id))
        stats = open(stats_log, 'w')

        # save start time and end time of test
        if self.test_start_time is not None and self.test_end_time is not None:
            test_run_duration = (
                'Start at: %s\nEnd at: %s\n' %
                (self.test_start_time, self.test_end_time))
            sys.stderr.write(test_run_duration)
            stats.write(test_run_duration)

        if self.mode == 'remote':
            ofst_info = ''
            if self.local_ofst is not None:
                ofst_info += 'Local clock offset: %s ms\n' % self.local_ofst

            if self.remote_ofst is not None:
                ofst_info += 'Remote clock offset: %s ms\n' % self.remote_ofst

            if ofst_info:
                sys.stderr.write(ofst_info)
                stats.write(ofst_info)

        stats.close()

    # run congestion control test
    def run(self):
        msg = 'Testing scheme %s for experiment run %d/%d...' % (
            self.cc, self.run_id, self.run_times)       # Testing scheme bbr for experiment run 1/1...
        sys.stderr.write(msg + '\n')                    # 开始测试

        # setup before running tests
        self.setup()

        # run receiver and sender
        if not self.run_congestion_control():           # 运行拥塞算法
            sys.stderr.write('Error in testing scheme %s with run ID %d\n' %
                             (self.cc, self.run_id))
            return

        # write runtimes and clock offsets to file
        self.record_time_stats()

        sys.stderr.write('Done testing %s\n' % self.cc)


def run_tests(args):
    # check and get git summary
    git_summary = utils.get_git_summary(args.mode,
                                        getattr(args, 'remote_path', None))     # 获取本地的 git 信息

    # get cc_schemes
    if args.all:                                            # 测试全部算法
        config = utils.parse_config()
        schemes_config = config['schemes']                  # 获取需要测试的算法

        cc_schemes = schemes_config.keys()                  # 从中获取 key, 也就是算法名称的集合
        if args.random_order:
            random.shuffle(cc_schemes)                      # 如果设置了随机顺序, 就打乱算法
    elif args.schemes is not None:                          # 没有设置 --all, 但设置了 --schemes "cubic bbr indigo"
        cc_schemes = args.schemes.split()                   # 分割得到算法列表 ["cubic", "bbr", "indigo"]
        if args.random_order:
            random.shuffle(cc_schemes)                      # 如果设置了随机顺序, 就打乱算法顺序
    else:
        assert(args.test_config is not None)                # 没有设置 --all 和 --schemes, 根据 --test_config 读取被测算法
        if args.random_order:
            random.shuffle(args.test_config['flows'])
        cc_schemes = [flow['scheme'] for flow in args.test_config['flows']]
        
    if(args.datapath != ''):
        args.data_dir = args.data_dir+'/'+args.datapath+'/'
    if not os.path.exists(args.data_dir):
        os.makedirs(args.data_dir)

    # save metadata
    meta = vars(args).copy()                                # 转换为 {} 字典
    meta['cc_schemes'] = sorted(cc_schemes)
    meta['git_summary'] = git_summary

    metadata_path = path.join(args.data_dir, 'pantheon_metadata.json')      # 存储 pantheon_metadata.json
    utils.save_test_metadata(meta, metadata_path)                           # 将 meta data 保存在 pantheon_metadata.json

    # run tests
    for run_id in range(args.start_run_id,
                         args.start_run_id + args.run_times):               # 运行测试, 从测试 id 开始, 运行次数为 run_times
        if not hasattr(args, 'test_config') or args.test_config is None:    # 没有配置 test_config
            for cc in cc_schemes:
                Test(args, run_id, cc).run()                                # 遍历待测量算法
        else:
            Test(args, run_id, None).run()


def pkill(args):
    sys.stderr.write('Cleaning up using pkill...'
                     '(enabled by --pkill-cleanup)\n')

    if args.mode == 'remote':
        r = utils.parse_remote_path(args.remote_path)
        remote_pkill_src = path.join(r['base_dir'], 'tools', 'pkill.py')

        cmd = r['ssh_cmd'] + ['python', remote_pkill_src,
                              '--kill-dir', r['base_dir']]
        call(cmd)

    pkill_src = path.join(context.base_dir, 'tools', 'pkill.py')
    cmd = ['python', pkill_src, '--kill-dir', context.src_dir]
    call(cmd)


def main():
    args = arg_parser.parse_test()                                      # 获取参数

    try:
        run_tests(args)                                                 # 运行
    except:  # intended to catch all exceptions
        # dump traceback ahead in case pkill kills the program
        sys.stderr.write(traceback.format_exc())

        if args.pkill_cleanup:
            pkill(args)

        sys.exit('Error in tests!')
    else:
        sys.stderr.write('All tests done!\n')


if __name__ == '__main__':
    main()
    
'''test.py 测试程序
参数配置:
    01. 使用 config 文件
    02. 使用命令行参数, 其中命令行参数可覆盖 config 配置
main:
    01. main() -> arg_parser.parse_test() 解析参数存储在 args
    02. run_tests(args) -> get_git_summary 获取本地 git 信息, 如使用 remote 测试, 则要求 local git 和 remote git 一致
        解析待测试的 schemes, 并执行 shuffle
    03. run_times 是测试运行的次数, 遍历 [start_run_id, start_run_id + run_times], 执行 Test(args, run_id, cc)
    04. Test.__init__(): 根据 remote/local 配置参数
    05. Test.run() -> setup()-> local:  setup_mm_cmd(): wrappers, first/second command, set up mm-link command (prepend/mm-link/append/extra)
                                remote: ssh_cmd
    06. Test.run_congestion_control() -> run_with_tunnel()
    07. run_tunnel_managers(): 子进程调用 tunnel_manager.py, 分别是 tunnel server manager (tsm) 和 tunnel client manager (tcm) 
            local mode: tcm 和 tsm 均运行在本地, 调用 tsm 和 tcm 只需要 python tunnel_manager.py 即可
            tunnel_manager 接受三种命令: prompt (设置提示语) / tunnel id (操作指定 id 的 mm-link) / halt (关闭全部 mm-link)
        创建 manager 进程, 等待 manager 输出 tunnel manager is running, 输入 'prompt [tsm]' 设置 manager 为 tsm
        创建 manager 进程, 等待 manager 输出 tunnel manager is running, 输入 'prompt [tcm]' 设置 manager 为 tcm
        返回两个进程的描述符
    08. run_with_tunnel(): local mode: sender->tcm, receiver->tsm, 先运行 receiver 再运行 sender
        run n flows, 对于每个流, 运行 run_tunnel_server 并分配一个独特的 tun_id
            -> run_tunnel_server(): 向 tsm 发送 tunnel tun_id mm-tunnelserver ..., 
                                    然后读取 tunnel tun_id readline 得到 mm-tunnelclient 命令
            -> run_tunnel_client(): 调用 run_tunnel_server() 返回的指令, 启动 mm-tunnelclient, 至此 mahimahi 隧道建立完毕 
    09. run_first_side():  运行 first side, 对于本地 bbr 测试, 就是先启动 iperf server, 并返回启动 second_side 的指令 (存储在list依次调用)
        run_second_side(): 遍历 list, 依次启动 bbr client, 也就是 iperf client.
'''
