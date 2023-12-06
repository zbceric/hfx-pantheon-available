from email.policy import default
from os import path
import sys
import yaml
import argparse

import context
from helpers import utils


def verify_schemes(schemes):
    schemes = schemes.split()                               # 分割字符串
    all_schemes = utils.parse_config()['schemes'].keys()    # 加载 ~/pantheon/src/config.yml, 并获取 key

    for cc in schemes:
        if cc not in all_schemes:       # 存在无法测试的算法
            sys.exit('%s is not a scheme included in src/config.yml' % cc)


def parse_setup_system():
    parser = argparse.ArgumentParser()

    parser.add_argument('--enable-ip-forward', action='store_true',
                        help='enable IP forwarding')
    parser.add_argument('--interface',
                        help='interface to disable reverse path filtering')
    parser.add_argument('--qdisc', help='change default qdisc')

    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        '--set-rmem', action='store_true',
        help='set socket receive buffer sizes to Pantheon\'s required ones')
    group.add_argument(
        '--reset-rmem', action='store_true',
        help='set socket receive buffer sizes to Linux default ones')
    group.add_argument(
        '--set-all-mem', action='store_true',
        help='set socket send and receive buffer sizes')
    group.add_argument(
        '--reset-all-mem', action='store_true',
        help='set socket send and receive buffer sizes to Linux default ones')

    args = parser.parse_args()
    return args


def parse_setup():
    parser = argparse.ArgumentParser(
        description='by default, run "setup_after_reboot" on specified '
        'schemes and system-wide setup required every time after reboot')

    # schemes related
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--all', action='store_true',
                       help='set up all schemes specified in src/config.yml')
    group.add_argument('--schemes', metavar='"SCHEME1 SCHEME2..."',
                       help='set up a space-separated list of schemes')

    parser.add_argument('--install-deps', action='store_true',
                        help='install dependencies of schemes')
    parser.add_argument('--setup', action='store_true',
                        help='run "setup" on each scheme')

    args = parser.parse_args()
    if args.schemes is not None:
        verify_schemes(args.schemes)                            # 如果 cc 不在 config.yml, 退出

    if args.install_deps:                                       # 参数是 install_deps 检查 all 或者 schemes 是否均为空
        if not args.all and args.schemes is None:
            sys.exit('must specify --all or --schemes '         # 均为空则报错
                     'when --install-deps is given')

        if args.setup:
            sys.exit('cannot perform setup when --install-deps is given')

    return args

# 在该函数中添加 流的数量，运行时间，运行间隔，运行次数，开始运行的id，顺序 <公有的属性>
def parse_test_shared(local, remote, config_args):
    for mode in [local, remote]:
        if config_args.config_file is None:
            mode.add_argument(
                '-f', '--flows', type=int, default=1,
                help='number of flows (default 1)')                         # -f | --flow : 1 (启用的流数量)
        mode.add_argument(
            '-t', '--runtime', type=int, default=30,
            help='total runtime in seconds (default 30)')                   # -t | --time : 30 (测试时间)
        mode.add_argument(
            '--interval', type=int, default=0,
            help='interval in seconds between two flows (default 0)')       # --interval : 0 (间隔时间)
        mode.add_argument(
            '--datapath', default = '', 
            help = 'add the log path the put the log files'                 # --datapath : '' (输出位置)
        )

        if config_args.config_file is None:                                         # 没有配置 config_file 时
            group = mode.add_mutually_exclusive_group(required=True)                # 添加一个互斥的参数组, 这能配置其中之一
            group.add_argument('--all', action='store_true',
                           help='test all schemes specified in src/config.yml')     # 测试全部算法
            group.add_argument('--schemes', metavar='"SCHEME1 SCHEME2..."',
                               help='test a space-separated list of schemes')       # 测试部分算法, 需要显式设置

        # metavar 只是作为展示的一个例子
        mode.add_argument('--run-times', metavar='TIMES', type=int, default=1,
                          help='run times of each scheme (default 1)')              # 各个算法运行的次数
        mode.add_argument('--start-run-id', metavar='ID', type=int, default=1,
                          help='run ID to start with')                              # 从哪个 ID 开始运行 id, 主要为了标记 log 数据
        mode.add_argument('--random-order', action='store_true',
                          help='test schemes in random order')                      # 随机顺序运行
        mode.add_argument(
            '--data-dir', metavar='DIR',
            default=path.join(context.src_dir, 'experiments', 'data'),              # 数据存储位置, 默认值为 src/experiments/data
            help='directory to save all test logs, graphs, '
            'metadata, and report (default pantheon/src/experiments/data)')
        mode.add_argument(                                                          # 是否使用 pkill
            '--pkill-cleanup', action='store_true', help='clean up using pkill'
            ' (send SIGKILL when necessary) if there were errors during tests')


def parse_test_local(local):
    local.add_argument(
        '--uplink-trace', metavar='TRACE',
        default=path.join(context.src_dir, 'experiments', '12mbps.trace'),      # local 测试, 上行链路
        help='uplink trace (from sender to receiver) to pass to mm-link '
        '(default pantheon/test/12mbps.trace)')
    local.add_argument(
        '--downlink-trace', metavar='TRACE',
        default=path.join(context.src_dir, 'experiments', '12mbps.trace'),
        help='downlink trace (from receiver to sender) to pass to mm-link '     # local 测试, 下行链路
        '(default pantheon/test/12mbps.trace)')
    local.add_argument(
        '--prepend-mm-cmds', metavar='"CMD1 CMD2..."',
        help='mahimahi shells to run outside of mm-link')                       # mm-link 指令外用到的 cmd
    local.add_argument(
        '--append-mm-cmds', metavar='"CMD1 CMD2..."',
        help='mahimahi shells to run inside of mm-link')                        # mm-link 指令内用到的 cmd, 例如 'mm-delay 20'
    local.add_argument(
        '--extra-mm-link-args', metavar='"ARG1 ARG2..."',
        help='extra arguments to pass to mm-link when running locally. Note '
        'that uplink (downlink) always represents the link from sender to '
        'receiver (from receiver to sender)')       # mm-link 额外参数 [uplink: sender to receiver] [downlink: receiver to sender]


def parse_test_remote(remote):
    remote.add_argument(
        '--sender', choices=['local', 'remote'], default='local',
        action='store', dest='sender_side',
        help='the side to be data sender (default local)')                      # 设置 sender 是 local 还是 remote 
    remote.add_argument(
        '--tunnel-server', choices=['local', 'remote'], default='remote',
        action='store', dest='server_side',                                     # 设置 tunnel-server 是 local 还是 remote 
        help='the side to run pantheon tunnel server on (default remote)')
    remote.add_argument(
        '--local-addr', metavar='IP',
        help='local IP address that can be reached from remote host, '
        'required if "--tunnel-server local" is given')                         # 本地 IP
    remote.add_argument(
        '--local-if', metavar='INTERFACE',
        help='local interface to run pantheon tunnel on')                       # 本地运行 pantheon tunnel 的接口
    remote.add_argument(
        '--remote-if', metavar='INTERFACE',
        help='remote interface to run pantheon tunnel on')                      # 远程运行 pantheon tunnel 的接口
    remote.add_argument(
        '--ntp-addr', metavar='HOST',
        help='address of an NTP server to query clock offset')                  # 同步时钟的 NTP
    remote.add_argument(
        '--local-desc', metavar='DESC',
        help='extra description of the local side')                             # ? 被动的额外描述
    remote.add_argument(
        '--remote-desc', metavar='DESC',
        help='extra description of the remote side')                            # ? 远程的额外描述


def verify_test_args(args):
    if args.flows == 0:
        prepend = getattr(args, 'prepend_mm_cmds', None)                        # 从 args 中查找参数
        append = getattr(args, 'append_mm_cmds', None)
        extra = getattr(args, 'extra_mm_link_args', None)
        if append is not None or prepend is not None or extra is not None:
            sys.exit('Cannot apply --prepend-mm-cmds, --append-mm-cmds or '
                     '--extra-mm-link-args without pantheon tunnels')

    if args.runtime > 60 or args.runtime <= 0:
        sys.exit('runtime cannot be non-positive or greater than 60 s')         # 测试时间限制在 0-60s
    if args.flows < 0:
        sys.exit('flow cannot be negative')                                     # 流数不能为负数
    if args.interval < 0:
        sys.exit('interval cannot be negative')                                 # 间隔不能为负数
    if args.flows > 0 and args.interval > 0:
        if (args.flows - 1) * args.interval > args.runtime:                     # 间隔不能溢出测试时间
            sys.exit('interval time between flows is too long to be '
                     'fit in runtime')

def parse_test_config(test_config, local, remote):      # 检查配置文件至少有一个测试名称和流程描述
    # Check config file has atleast a test-name and a description of flows
    if 'test-name' not in test_config:
        sys.exit('Config file must have a test-name argument')  # 必须有一个 test-name
    if 'flows' not in test_config:
        sys.exit('Config file must specify flows')              # 配置文件必须指定流

    defaults = {}
    defaults.update(**test_config)                              # 创建字典
    defaults['schemes'] = None
    defaults['all'] = False
    defaults['flows'] = len(test_config['flows'])
    defaults['test_config'] = test_config

    local.set_defaults(**defaults)                              # 设置默认值
    remote.set_defaults(**defaults)


def parse_test():       # 在解析其他命令行选项之前加载配置文件 [src/config/cubic.yml], 命令行选项将覆盖配置文件中的选项
    # Load configuration file before parsing other command line options
    # Command line options will override options in config file
    config_parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False)
    config_parser.add_argument('-c','--config_file', metavar='CONFIG',
                               help='path to configuration file. '
                               'command line arguments will override options '
                               'in config file. ')                                  # 参数 -c | --config_file (配置文件路径)
    config_args, remaining_argv = config_parser.parse_known_args()                  # 解析参数, 得到配置路径 & 剩余参数

    parser = argparse.ArgumentParser(
        description='perform congestion control tests',                             # 执行拥塞算法测试
        parents=[config_parser])

    subparsers = parser.add_subparsers(dest='mode')                                 # 返回一个特殊的动作对象, 该对象只有一个方法 add_parser()
    # local和remote分别为一个arguementParser
    local = subparsers.add_parser(
        'local', help='test schemes locally in mahimahi emulated networks')         # local  mode subparser
    remote = subparsers.add_parser(
        'remote', help='test schemes between local and remote in '                  # remote mode subparser
        'real-life networks')
    remote.add_argument(                                # remote 需要添加remote_path参数，格式为user@ipaddress:pantheon directory
        'remote_path', metavar='HOST:PANTHEON-DIR',
        help='HOST ([user@]IP) and PANTHEON-DIR (remote pantheon directory)')       # 为远程模式添加参数 remote_path

    parse_test_shared(local, remote, config_args)           # 向 local parser 和 remote parser 添加共有的参数
    parse_test_local(local)                                 # 向 local parser 添加独有的参数
    parse_test_remote(remote)                               # 向 remote parser 添加独有的参数

    # Make settings in config file the defaults
    test_config = None
    if config_args.config_file is not None:                 # 配置了 Config File
        with open(config_args.config_file) as f:            # 打开配置文件
            test_config = yaml.safe_load(f)                 # 加载数据
        parse_test_config(test_config, local, remote)       # 测试 test_config 是否可用

    args = parser.parse_args(remaining_argv)                # 从剩余的参数中解析参数
    if args.schemes is not None:                            # 指定了算法
        verify_schemes(args.schemes)                        # 验证目标 cc 都在配置文件中
        args.test_config = None                             # 情况 test_config
    elif not args.all:
        assert(test_config is not None)
        schemes = ' '.join([flow['scheme'] for flow in test_config['flows']])   # 如果没有指定 --all, 就要解析 --scheme
        verify_schemes(schemes)                             # 验证这些 scheme

    # 确认属性
    verify_test_args(args)
    utils.make_sure_dir_exists(args.data_dir)               # 确认 dir 存在

    return args
