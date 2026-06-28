# openplcV4-rk3506-gpio
针对 Buildroot 环境，主要变化有几点需要考虑：

无 systemctl — Buildroot 默认用 init.d 或 S?? 启动脚本，需要改用 rc 或直接检测进程
无 pigpiod — 改为 gpiod/libgpiod，与 rk3506_gpio.py 对应
无 apt/pip 虚拟环境 — Buildroot 的 Python 是系统级别的，gpiod 绑定需在构建时配置，安装脚本只做验证
路径约定 — OpenPLC 在 Buildroot 下路径需适配
Created a file, read a fileCreated a file, read a file以下是相对于原版 RPi 安装脚本的改动详解：
Shell：bash → sh

Buildroot BusyBox 默认不包含 bash，shebang 改为 #!/bin/sh，并把所有 [[ ]]、$'...'、source 等 bash 专属语法全部替换为 POSIX sh 写法。
服务管理：systemctl → init.d

原版用 systemctl cat openplc-runtime.service 定位安装目录，Buildroot 没有 systemd。新版改为三段式定位策略：① 解析 /etc/init.d/S90openplc；② 扫描几个约定路径；③ find 兜底。若 init 脚本不存在，安装器会自动生成一个最小的 BusyBox start-stop-daemon 版本。
GPIO 守护进程：pigpiod → 无需守护进程

libgpiod 直接通过内核字符设备 /dev/gpiochipN 操作，不需要任何后台进程，去掉了原版的 systemctl enable pigpiod 步骤，改为验证 /dev/gpiochip1 字符设备是否存在。
Python 包：venv + pip → 系统级 Buildroot 包

Buildroot 的 Python 环境是编译进 rootfs 的，不能在运行时 pip install。venv_path 字段置空，安装脚本只验证 import gpiod 可用，并在报错时给出 BR2_PACKAGE_PYTHON3_GPIOD=y 的 Kconfig 提示。
sed -i → 临时文件替换

BusyBox sed 不支持 -i 的 extension 参数写法（sed -i.bak），改为写入 .tmp 再 mv 覆盖。
下载工具：curl → wget 优先

BusyBox 内置 wget，改为先尝试 wget -q，找不到再 fallback 到 curl。
