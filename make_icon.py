# -*- coding: utf-8 -*-
"""生成 WorkBuddy 签到助手应用图标：蓝紫渐变圆角方块 + 日历 + 对勾"""
from PIL import Image, ImageDraw

S = 1024  # 画布（超大，向下采样保证小尺寸清晰）
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# 圆角方块渐变（对角线：#5B8DEF -> #8B5CF6）
grad = Image.new("RGB", (S, S))
gd = ImageDraw.Draw(grad)
c1, c2 = (91, 141, 239), (139, 92, 246)
for y in range(S):
    for x in range(0, S, 8):
        t = (x + y) / (2 * S)
        c = tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))
        gd.rectangle([x, y, x + 8, y], fill=c)
mask = Image.new("L", (S, S), 0)
md = ImageDraw.Draw(mask)
md.rounded_rectangle([28, 28, S - 28, S - 28], radius=int(S * 0.22), fill=255)
img.paste(grad, (0, 0), mask)

d = ImageDraw.Draw(img)
W = (255, 255, 255, 255)

# 日历（圆角矩形 + 顶部横条 + 两个挂环）
cal_l, cal_t, cal_r, cal_b = int(S*0.20), int(S*0.26), int(S*0.80), int(S*0.80)
d.rounded_rectangle([cal_l, cal_t, cal_r, cal_b], radius=int(S*0.07), fill=W)
# 顶部条（用渐变底色不透明块模拟挖空）
bar_h = int(S*0.10)
d.rounded_rectangle([cal_l, cal_t, cal_r, cal_t + bar_h], radius=int(S*0.07), fill=(0,0,0,0))
d.rectangle([cal_l, cal_t + bar_h//2, cal_r, cal_t + bar_h], fill=(0,0,0,0))
# 挂环
ring_w, ring_h = int(S*0.045), int(S*0.10)
for cx in (int(S*0.36), int(S*0.64)):
    d.rounded_rectangle([cx - ring_w//2, cal_t - int(S*0.05), cx + ring_w//2, cal_t + ring_h - int(S*0.05)],
                        radius=ring_w//2, fill=W)
# 对勾（粗线，日历中部，用主题蓝）
blue = (91, 111, 240, 255)
p1 = (int(S*0.34), int(S*0.585))
p2 = (int(S*0.465), int(S*0.71))
p3 = (int(S*0.68), int(S*0.46))
w = int(S*0.055)
d.line([p1, p2], fill=blue, width=w)
d.line([p2, p3], fill=blue, width=w)
# 线帽圆角
for p in (p1, p2, p3):
    d.ellipse([p[0]-w//2, p[1]-w//2, p[0]+w//2, p[1]+w//2], fill=blue)

sizes = [(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)]
img.save("app.ico", sizes=sizes)
img.resize((256,256), Image.LANCZOS).save("app_icon_256.png")
print("icon ok")
