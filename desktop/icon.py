import zlib, struct, math
W=H=512
img=bytearray(W*H*4)
margin, radius = 40, 112
def lerp(a,b,t): return tuple(int(a[i]+(b[i]-a[i])*t) for i in range(3))
INDIGO=(124,92,255); CYAN=(34,211,238); BG=(13,17,24)
def rr_sdf(x,y):
    hw=(W-2*margin)/2; hh=(H-2*margin)/2
    px=abs(x-W/2)-(hw-radius); py=abs(y-H/2)-(hh-radius)
    dx=max(px,0); dy=max(py,0)
    return math.hypot(dx,dy)+min(max(px,py),0)-radius
cx,cy=W/2,H/2
for y in range(H):
    for x in range(W):
        i=(y*W+x)*4
        d=rr_sdf(x,y)
        a=max(0.0,min(1.0,0.5-d))   # AA edge of the card
        if a<=0: continue
        t=(x+y)/(W+H)
        r,g,b=lerp(INDIGO,CYAN,t)
        # disk platter: white ring + hub, orbit dot (satellite motif)
        dist=math.hypot(x-cx,y-cy)
        ring=abs(dist-150)
        if ring<16: r=g=b=245                      # platter ring
        elif dist<34: r=g=b=245                    # hub
        ang=math.atan2(y-cy,x-cx)
        ox,oy=cx+205*math.cos(-0.7), cy+205*math.sin(-0.7)
        if math.hypot(x-ox,y-oy)<26: r=g=b=245     # satellite
        img[i]=r; img[i+1]=g; img[i+2]=b; img[i+3]=int(a*255)
# write PNG
def chunk(t,d): 
    c=t+d; return struct.pack(">I",len(d))+c+struct.pack(">I",zlib.crc32(c)&0xffffffff)
raw=bytearray()
for y in range(H):
    raw.append(0); raw+=img[y*W*4:(y+1)*W*4]
png=b"\x89PNG\r\n\x1a\n"+chunk(b"IHDR",struct.pack(">IIBBBBB",W,H,8,6,0,0,0))+chunk(b"IDAT",zlib.compress(bytes(raw),9))+chunk(b"IEND",b"")
import sys
out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/gdf_icon.png"
open(out,"wb").write(png)
print("png written", len(png), "bytes ->", out)
