#!/usr/bin/env python

from subprocess import check_call

import arg_parser
import context
from helpers import kernel_ctl


def setup_bbr():
    # load tcp_bbr kernel module (only available since Linux Kernel 4.9)
    kernel_ctl.load_kernel_module('tcp_bbr')

    # add bbr to kernel-allowed congestion control list
    kernel_ctl.enable_congestion_control('bbr')

    # check if qdisc is fq
    # kernel_ctl.check_qdisc('fq')
    kernel_ctl.check_qdisc('fq_codel')          # Linux 4.x 使用的是 fq, Linux5.x 使用的是 fq_codel


def main():
    args = arg_parser.receiver_first()          # 先启动 receiver, 这里直接输出, 调用的程序会通过管道获取输出值

    if args.option == 'deps':
        print('iperf')
        return

    if args.option == 'setup_after_reboot':
        setup_bbr()
        return

    if args.option == 'receiver':               # iperf -Z bbr, -s 作为 server, -p port
        cmd = ['iperf', '-Z', 'bbr', '-s', '-p', args.port]
        check_call(cmd)
        return

    if args.option == 'sender':                 # iperf -Z bbr 使用 bbr, -c ip 作为 client, 绑定 ip, -p port, -t 运行时间
        cmd = ['iperf', '-Z', 'bbr', '-c', args.ip, '-p', args.port,
               '-t', '75']
        check_call(cmd)
        return


if __name__ == '__main__':
    main()
