#!/bin/bash

# 记录当前地址
cur_dir=$PWD

# 更新子模块
git submodule update --init --recursive

# 安装需要的依赖项
sudo apt install -y gcc g++ autoconf make
sudo apt install -y wget
sudo apt install -y python-dev python3-dev libxml2-dev libxslt1-dev zlib1g-dev \ 
	            libsasl2-dev libldap2-dev build-essential libssl-dev libffi-dev \
		    libmysqlclient-dev libjpeg-dev libpq-dev libjpeg8-dev 
		    liblcms2-dev libblas-dev libatlas-base-dev
		   
sudo apt-get install -y mahimahi ntp ntpdate texlive

# 安装 python 所需的包, 要求python版本在3.5-3.7之间
pip install pyyaml google
conda install -c aaronzs tensorflow-gpu==1.15.0

# 安装 pantheon-tunnel
cd $cur_dir/third_party/pantheon-tunnel
./autogen.sh
./configure CXXFLAGS="-Wno-error"
make -j 8
sudo make install

# 设置 QUIC 路径
export PROTO_QUIC_ROOT=$cur_dir/third_party/proto-quic/src
export PATH="$cur_dir/third_party/proto-quic/dev_tools:$PATH"

# setup
cd $curdir
src/experiments/setup.py --install-deps --all		# 可以忽略 MixedTest.py 错误
src/experiments/setup.py --setup --all
