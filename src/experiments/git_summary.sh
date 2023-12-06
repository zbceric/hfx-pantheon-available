#!/bin/sh

echo -n 'branch: '                                  # 打印 "branch ", 不换行
git rev-parse --abbrev-ref @ | head -c -1           # 打印当前分支名称
echo -n ' @ '                                       # 打印 " @ "
git rev-parse @                                     # 打印仓库信息
git submodule foreach --quiet 'echo $path @ `git rev-parse @`; git status -s --untracked-files=no --porcelain'  # 遍历打印子模块信息
