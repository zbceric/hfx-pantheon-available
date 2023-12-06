#!/usr/bin/env python

import os
from os import path
import sys
import signal
from subprocess import Popen, PIPE

import context
from helpers import utils


def main():
    prompt = ''
    procs = {}

    # register SIGINT and SIGTERM events to clean up gracefully before quit
    def stop_signal_handler(signum, frame):
        for tun_id in procs:
            utils.kill_proc_group(procs[tun_id])                    # 关闭全部 tunnel

        sys.exit('tunnel_manager: caught signal %s and cleaned up\n' % signum)

    signal.signal(signal.SIGINT, stop_signal_handler)               # SIGTERM 关闭程序信号
    signal.signal(signal.SIGTERM, stop_signal_handler)              # 接收 ctrl+c 信号

    sys.stdout.write('tunnel manager is running\n')                 # 正在运行 tunnel
    sys.stdout.flush()


    while True:
        input_cmd = sys.stdin.readline().strip()                    
        # command 1. prompt    [len = 2]        打印提示信息
        #            usage:   'prompt tsm\n'    打印 [tsm]
        # command 2. tunnel id [len > 2]        与 tunnel 相关的指令, 其中 id 是 tunnel 的编号, 启动的 tunnel 进程存储在 procs[id]
        #         2.1   tunnel id mm-tunnelclient ...   启动 mm-tunnel client
        #         2.2   tunnel id mm-tunnelserver ...   启动 mm-tunnel server
        #         2.3   tunnel id python ...    在 mm-tunnel 进程内执行 python 脚本, 将 cmd 传入进程 procs[id] 执行
        #         2.3.1 tunnel id python src/wrappers/bbr.py receiver       启动 bbr 的 receiver
        #         2.4   tunnel id readline      从 procs[id] 中读取一行, 然后输入自己的标准输出, tunnel_manager 的创建者就可以读取 procs[id] 的输出了
        # command 3. halt      [len = 1]        挂起, 将全部 procs[id] 进程终止

        # print all the commands fed into tunnel manager
        if prompt:
            sys.stderr.write(prompt + ' ')
        sys.stderr.write(input_cmd + '\n')
        cmd = input_cmd.split()

        # manage I/O of multiple tunnels
        if cmd[0] == 'tunnel':
            if len(cmd) < 3:
                sys.stderr.write('error: usage: tunnel ID CMD...\n')
                continue

            try:
                tun_id = int(cmd[1])                    # 第二个参数是 tun_id
            except ValueError:
                sys.stderr.write('error: usage: tunnel ID CMD...\n')
                continue

            cmd_to_run = ' '.join(cmd[2:])
            if cmd[2] == 'mm-tunnelclient' or cmd[2] == 'mm-tunnelserver':
        
                # expand env variables (e.g., MAHIMAHI_BASE) 用环境变量替换
                cmd_to_run = path.expandvars(cmd_to_run).split()

                
                # expand home directory
                for i in range(len(cmd_to_run)):
                    if ('--ingress-log' in cmd_to_run[i] or
                        '--egress-log' in cmd_to_run[i]):
                        t = cmd_to_run[i].split('=')
                        cmd_to_run[i] = t[0] + '=' + path.expanduser(t[1])
                
                # cmd_to_run[0] = 'mm-delay 30 '+ cmd_to_run[0]
                # tun_id fd
                procs[tun_id] = Popen(cmd_to_run, stdin=PIPE,
                                      stdout=PIPE, preexec_fn=os.setsid)        # 开启子进程
            elif cmd[2] == 'python':  # run python scripts inside tunnel
                if tun_id not in procs:
                    sys.stderr.write(
                        'error: run tunnel client or server first\n')

                procs[tun_id].stdin.write((cmd_to_run + '\n').encode('utf-8'))
                procs[tun_id].stdin.flush()
            elif cmd[2] == 'readline':  # readline from stdout of tunnel        # 从 tunnel 的标准输出读取一行数据
                if len(cmd) != 3:
                    sys.stderr.write('error: usage: tunnel ID readline\n')
                    continue

                if tun_id not in procs:
                    sys.stderr.write(
                        'error: run tunnel client or server first\n')

                sys.stdout.write(procs[tun_id].stdout.readline().decode('utf-8'))
                sys.stdout.flush()
            else:
                sys.stderr.write('unknown command after "tunnel ID": %s\n'
                                 % cmd_to_run)
                continue
        elif cmd[0] == 'prompt':  # set prompt in front of commands to print
            if len(cmd) != 2:
                sys.stderr.write('error: usage: prompt PROMPT\n')
                continue

            prompt = cmd[1].strip()
        elif cmd[0] == 'halt':  # terminate all tunnel processes and quit   挂起
            if len(cmd) != 1:
                sys.stderr.write('error: usage: halt\n')
                continue

            for tun_id in procs:
                utils.kill_proc_group(procs[tun_id])                # 杀掉全部 tunnel 进程

            sys.exit(0)
        else:
            sys.stderr.write('unknown command: %s\n' % input_cmd)
            continue


if __name__ == '__main__':
    main()
