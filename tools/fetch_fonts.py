"""
把报告页用到的 Barlow / Barlow Condensed 从 Google Fonts 抓成本地文件。

为什么要自托管：国内网络访问不了 fonts.googleapis.com，而模板里那个
<link rel="stylesheet"> 是阻塞渲染的 —— 报告页会白屏等到超时（实测 ASUS 上 9.7 秒）。
自托管之后字体和页面同源，断网也不影响。

只保留 latin 子集：中文走系统的 Microsoft YaHei，Google 的其他子集
（cyrillic / vietnamese 等）这个报告用不到，留着白白多十几个文件。

在能访问 Google 的机器上跑（OCI 新加坡可以，ASUS 不行）：
    python3 fetch_fonts.py assets/fonts
"""

import os
import re
import sys
import urllib.request

CSS_URL = ("https://fonts.googleapis.com/css2"
           "?family=Barlow:wght@400;500;600;700"
           "&family=Barlow+Condensed:wght@400;600&display=swap")

# 不带现代浏览器 UA 的话 Google 会返回 ttf 而不是 woff2
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def main(outdir):
    os.makedirs(outdir, exist_ok=True)
    css = get(CSS_URL).decode("utf-8")

    # Google 用 /* latin */ 这样的注释标注每个 @font-face 属于哪个子集
    blocks = re.split(r"(?=/\*)", css)
    kept, urls = [], set()
    for b in blocks:
        if "@font-face" not in b:
            continue
        if not re.match(r"/\*\s*latin\s*\*/", b.strip()):
            continue
        for u in re.findall(r"url\((https://[^)]+\.woff2)\)", b):
            urls.add(u)
            b = b.replace(u, "./" + u.rsplit("/", 1)[-1])
        kept.append(b.strip())

    for u in sorted(urls):
        name = u.rsplit("/", 1)[-1]
        path = os.path.join(outdir, name)
        if not os.path.exists(path):
            with open(path, "wb") as f:
                f.write(get(u))
            print("  下载 %s" % name)

    header = ("/* 由 fetch_fonts.py 生成，勿手改。\n"
              "   只含 latin 子集；中文由 font-family 里的系统字体接管。 */\n")
    with open(os.path.join(outdir, "fonts.css"), "w", encoding="utf-8") as f:
        f.write(header + "\n\n".join(kept) + "\n")

    total = sum(os.path.getsize(os.path.join(outdir, n))
                for n in os.listdir(outdir))
    print("共 %d 个字体文件，合计 %.1f KB" % (len(urls), total / 1024))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "assets/fonts")
