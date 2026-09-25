"""Render the latest fixed-Q3 fourth-question result as a Chinese infographic."""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parent
OUT = ROOT / "results_q3_q4_timing" / "Q4_结果分析图.png"
W, H = 1500, 2020
NAVY, BLUE, GREEN = "#123D68", "#168BC0", "#1B9365"
INK, MUTED, GRID = "#17314D", "#5D7288", "#CADCE9"
PALE, PALE_GREEN, WHITE, RED = "#EEF7FC", "#EEF9F3", "#FFFFFF", "#E64646"


def font(size, bold=False):
    paths = (["C:/Windows/Fonts/msyhbd.ttc", "C:/Windows/Fonts/simhei.ttf"] if bold else
             ["C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf"])
    for path in paths:
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


F18, F21, F24, F28, F34, F48 = [font(x) for x in (18, 21, 24, 28, 34, 48)]
B20, B24, B28, B34, B48 = [font(x, True) for x in (20, 24, 28, 34, 48)]


def rounded(draw, box, fill, outline=GRID, radius=12, width=2):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def section(draw, y, number, title, color=BLUE):
    rounded(draw, (24, y, W-24, y+54), PALE, outline=GRID)
    draw.rounded_rectangle((24, y, 595, y+54), radius=10, fill=color)
    draw.text((50, y+8), f"{number}. {title}", fill=WHITE, font=B28)


def fit(draw, text, box, fnt, fill=INK, line_gap=8):
    x0,y0,x1,y1=box; words=list(text); lines=[]; line=""
    for ch in words:
        test=line+ch
        if draw.textbbox((0,0),test,font=fnt)[2] <= x1-x0:
            line=test
        else:
            lines.append(line);line=ch
    if line:lines.append(line)
    step=fnt.size+line_gap
    y=y0+(y1-y0-len(lines)*step+line_gap)/2
    for line in lines:
        draw.text((x0,y),line,font=fnt,fill=fill);y+=step


def bar_panel(draw, box, title, values, max_value, colors, suffix=""):
    x0,y0,x1,y1=box;rounded(draw,box,WHITE)
    draw.text((x0+22,y0+18),title,font=B24,fill=INK)
    base=x0+185; right=x1-35
    for i,(label,value) in enumerate(values):
        y=y0+82+i*73
        draw.text((x0+22,y+7),label,font=F21,fill=INK)
        width=(right-base)*value/max_value
        draw.rounded_rectangle((base,y,base+width,y+38),radius=6,fill=colors[i])
        draw.text((base+width+10,y+5),f"{value:g}{suffix}",font=B20,fill=INK)


im=Image.new("RGB",(W,H),"#F7FBFE");d=ImageDraw.Draw(im)
d.text((W/2,30),"问题四结果分析图表：两组方案 vs 三组方案",anchor="ma",font=B48,fill=NAVY)
d.text((W/2,92),"固定第三问组批、访问顺序、任务时序与实际通信关系后的资源配置比较",anchor="ma",font=F24,fill=INK)

section(d,140,1,"核心结论")
conclusions=[
    "固定当前1 dB通信方案后无零缺口分区：P3缺2件，唯一三组P4缺8件。",
    "两种方案均配送80箱；31箱硬时限货箱全部按时送达，连续通信中断时长为0。",
    "现有任务只形成3个不可拆块；P3单列S011可少购资源，但组间负荷悬殊。"]
for i,text in enumerate(conclusions):
    y=211+i*62
    d.ellipse((61,y,105,y+44),fill=BLUE);d.text((83,y+5),str(i+1),anchor="ma",font=B24,fill=WHITE)
    d.text((130,y+6),text,font=F24,fill=INK)

section(d,420,2,"两组方案与三组方案的详细对比")
left,right=25,1475;top=480
col=[left,410,950,right]
headers=["指标","两组方案 P3（缺口优先）","三组方案 P4"]
for i in range(3):
    d.rectangle((col[i],top,col[i+1],top+60),fill=(NAVY,BLUE,GREEN)[i])
    d.text(((col[i]+col[i+1])/2,top+14),headers[i],anchor="ma",font=B24,fill=WHITE)
rows=[
    ("分组结果","G1＝除S011外的14区\nG2＝S011","G1＝S001\nG2＝13区大任务块\nG3＝S011"),
    ("运输无人机（A/B/C）","4 / 3 / 2","5 / 3 / 4"),
    ("共享电池（A/B/C）","6 / 5 / 4","7 / 5 / 6"),
    ("中继机 / 能源组件","2 / 3","2 / 3"),
    ("资源需求总数","29","35"),
    ("资源缺口（单位）","2（B机1、B电池1）","8"),
    ("工作量变异系数 CV","0.957859","1.209716"),
    ("总能耗（kWh）","72.675908","72.675908"),
    ("硬时限货箱按时送达","31 / 31","31 / 31"),
    ("通信中断时长","0 s","0 s")]
y=top+60
for idx,(name,a,b) in enumerate(rows):
    h=130 if idx==0 else 58
    fills=("#F3F8FC",PALE,PALE_GREEN)
    for i,text in enumerate((name,a,b)):
        d.rectangle((col[i],y,col[i+1],y+h),fill=fills[i],outline=GRID,width=1)
        fit(d,text,(col[i]+14,y+6,col[i+1]-14,y+h-6),B20 if i==0 else (F18 if idx==0 else F21))
    y+=h

section(d,1190,3,"关键指标可视化对比")
bar_panel(d,(25,1250,485,1560),"资源缺口（越低越好）",[("两组P3",2),("三组P4",8)],12,[BLUE,GREEN],"件")
bar_panel(d,(520,1250,980,1560),"资源总数（越低越好）",[("两组P3",29),("三组P4",35)],45,[BLUE,GREEN],"件")
bar_panel(d,(1015,1250,1475,1560),"工作量CV（越低越均衡）",[("两组P3",.957859),("三组P4",1.209716)],1.9,[BLUE,GREEN])
d.rounded_rectangle((40,1575,W-40,1630),radius=24,fill="#E0F1FA")
d.text((W/2,1586),"P3仅在缺口优先规则下推荐；两组P2更均衡（CV 0.805），但缺8件。",anchor="ma",font=B24,fill=NAVY)

section(d,1650,4,"结论与建议")
rounded(d,(25,1710,730,1890),PALE,outline="#9CCBE4")
d.rectangle((25,1710,730,1762),fill=BLUE)
d.text((377,1722),"选择两组P3的情形",anchor="ma",font=B24,fill=WHITE)
for i,t in enumerate(("优先减少采购与资源占用","可补充1架B型机和1组B型电池","接受G1承担约97.9%的总工作量")):
    d.ellipse((60,1781+i*34,78,1799+i*34),fill=BLUE);d.text((94,1776+i*34),t,font=F21,fill=INK)
rounded(d,(770,1710,1475,1890),PALE_GREEN,outline="#9BD4BC")
d.rectangle((770,1710,1475,1762),fill=GREEN)
d.text((1122,1722),"采用三组P4的情形",anchor="ma",font=B24,fill=WHITE)
for i,t in enumerate(("必须建立三个资源完全独立的任务组","能够补齐8件逐类型资源缺口","接受资源更多且当前CV更高")):
    d.ellipse((805,1781+i*34,823,1799+i*34),fill=GREEN);d.text((839,1776+i*34),t,font=F21,fill=INK)
d.rounded_rectangle((25,1910,W-25,1980),radius=24,fill="#DCEFFA")
d.text((W/2,1928),"结论：缺口优先选P3；均衡优先考虑P2；真正改善失衡需重做Q3任务绑定。",anchor="ma",font=B24,fill=NAVY)

OUT.parent.mkdir(parents=True,exist_ok=True)
im.save(OUT,optimize=True)
print(OUT)
