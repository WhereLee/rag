#!/bin/bash
# 安全收尾：ufw（先放行 22/80/443 再 enable）+ SSH 禁密码登录（sshd -t 校验 + 备份）
set -e

echo "== 1/3 ufw =="
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw --force enable
sudo ufw status | head -8

echo "== 2/3 sshd 禁密码 =="
sudo cp /etc/ssh/sshd_config /etc/ssh/sshd_config.bak.$(date +%Y%m%d)
if grep -q '^PasswordAuthentication' /etc/ssh/sshd_config; then
  sudo sed -i 's/^PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
else
  echo 'PasswordAuthentication no' | sudo tee -a /etc/ssh/sshd_config
fi
sudo sshd -t && echo "sshd config OK"
sudo systemctl restart sshd
sleep 2
sudo systemctl is-active sshd

echo "== 3/3 验证 =="
sudo ufw status verbose | head -10
echo "== DONE =="
