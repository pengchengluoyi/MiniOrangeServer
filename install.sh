#!/bin/bash
DIR="$(pwd)/lib"

pip uninstall ab_ability -y
pip uninstall ab_script -y


# 使用find命令，-type f表示只找文件，-name "*.txt"表示匹配.txt后缀
find "$DIR" -type f -name "*.whl" | while read file; do
    echo "Processing: $file"
    pip install $file
    # 在这里添加处理命令
done