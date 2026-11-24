#!/bin/bash
pip3 install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
python3 mock_robot_server.py &
SERVER_PID=$!
sleep 2
python3 main.py
kill $SERVER_PID
