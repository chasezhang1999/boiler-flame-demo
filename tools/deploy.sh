#!/usr/bin/env bash
#
# 把本机的改动发布到服务器。一条命令走完：提交 → 推 GitHub → 服务器拉取 → 重建镜像 → 体检。
#
#   bash tools/deploy.sh                改动已经提交过了，只发布
#   bash tools/deploy.sh "改了什么"      顺手把当前改动提交上去，再发布
#
# 中途断了（SSH 抖动会发生）直接重跑，每一步都可以重复执行，不会做坏。
#
set -euo pipefail

SERVER=ubuntu@140.245.117.205
KEY=~/.ssh/oracle_sg
DIR=/home/ubuntu/flame-demo
URL=https://flame.518521.xyz

# SSH 到这台 OCI 偶尔会断，加心跳；断了也不怕，重跑即可
SSH="ssh -i $KEY -o ServerAliveInterval=15 -o ConnectTimeout=20"

step() { printf '\n=== [%s/5] %s\n' "$1" "$2"; }
die()  { printf '\n!! %s\n' "$1" >&2; exit 1; }

cd "$(dirname "$0")/.."

# ---------------------------------------------------------------- 1. 提交
step 1 "检查本机改动"
if [ -n "$(git status --porcelain)" ]; then
  if [ $# -eq 0 ]; then
    git status --short
    die "上面这些改动还没提交。要么先自己 git commit，要么给个说明重跑：
     bash tools/deploy.sh \"改了什么\""
  fi
  echo "将要提交下面这些文件："
  git status --short
  # 公开仓库，提交前让人眼过一遍，免得把不该进的东西推上去
  printf '\n确认提交并推送到 GitHub？[y/N] '
  read -r yes
  [ "$yes" = "y" ] || [ "$yes" = "Y" ] || die "已取消，什么都没做。"
  git add -A
  git commit -q -m "$1"
  echo "已提交：$(git log --oneline -1)"
else
  echo "工作区干净，直接发布已提交的内容。"
fi

# ---------------------------------------------------------------- 2. 推送
step 2 "推送到 GitHub"
git push origin main
LOCAL=$(git rev-parse --short HEAD)
echo "GitHub main 现在是 $LOCAL"

# ---------------------------------------------------------------- 3. 拉取
# 服务器上那个目录是 GitHub 的一份检出。用 reset --hard 而不是 pull：
# 服务器上不该有任何本地改动，有也一律以 GitHub 为准，省得哪天冲突了卡在那里。
# .env / data / static / inbox 都在 .gitignore 里，reset 碰不到它们。
step 3 "服务器拉取代码"
$SSH "$SERVER" "cd $DIR && git fetch -q origin main && git reset -q --hard origin/main && git rev-parse --short HEAD"

# ---------------------------------------------------------------- 4. 重建
# 代码是 COPY 进镜像的（见 Dockerfile），不是挂载。所以改了代码必须 --build，
# 光 restart 是跑不出新代码的。
step 4 "重建镜像并重启（约一两分钟）"
$SSH "$SERVER" "cd $DIR && docker compose up -d --build" 2>&1 | grep -v '^#' || true

# ---------------------------------------------------------------- 5. 体检
step 5 "体检"
sleep 4
$SSH "$SERVER" "docker ps --filter name=flame-cv --format '容器  {{.Status}}'"
REMOTE=$($SSH "$SERVER" "cd $DIR && git rev-parse --short HEAD")
CODE=$(curl -s -o /dev/null -m 20 -w '%{http_code}' "$URL/")

echo "本机 $LOCAL   服务器 $REMOTE"
[ "$LOCAL" = "$REMOTE" ] || die "版本对不上，服务器没拉到最新代码。"
[ "$CODE" = "200" ]      || die "$URL 返回 $CODE，服务没起来。用这条看日志：
     ssh -i $KEY $SERVER 'docker logs --tail 50 flame-cv'"

printf '\n发布完成：%s 已经是 %s\n' "$URL" "$LOCAL"
