#!/bin/bash
cd /root/polymarket-bot/src
export SILICON_FLOW_KEY="sk-bufsosefgfgzaklxbrijjxebarykoyfaxqcmalftvohamlub"

while true; do
    python3 fetch_markets.py
    
    # 2. 竞技场预测
    python3 predict.py --cycle $(date +%s)
    
    # 3. 结算已结束市场
    python3 score.py
    
    echo "--- Cycle Complete. Waiting 5 minutes... ---"
    sleep 300
done
