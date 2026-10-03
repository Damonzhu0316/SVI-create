"""Local BD09LL / BD09MC polynomial transforms (gcoord coefficients, MIT).

BD09 is NOT WGS84 or EPSG:3857. Distance uses inverse BD09/GCJ and WGS84 geodesics.
"""
import json, math
from pathlib import Path
from pyproj import Geod
COEF=json.loads(Path(__file__).with_name('bd09_coefficients.json').read_text())
def _convert(x,y,c):
    v=abs(y)/c[9]
    return math.copysign(c[0]+c[1]*abs(x),x), math.copysign(sum(c[i+2]*v**i for i in range(7)),y)
def bd09ll_to_mc(lon,lat):
    if not all(map(math.isfinite,(lon,lat))) or not (-180<=lon<=180 and -74<=lat<=74):
        raise ValueError('Invalid BD09LL coordinate')
    i=next(i for i,b in enumerate(COEF['LLBAND']) if abs(lat)>=b)
    return _convert(lon,lat,COEF['LL2MC'][i])
def bd09mc_to_ll(x,y):
    if not all(map(math.isfinite,(x,y))) or abs(x)>21000000 or abs(y)>13000000:
        raise ValueError('Invalid BD09MC coordinate or unexpected coordinate scale')
    i=next(i for i,b in enumerate(COEF['MCBAND']) if abs(y)>=b)
    return _convert(x,y,COEF['MC2LL'][i])
def wgs84_to_gcj02(lon,lat):
    if not (72.004<=lon<=137.8347 and .8293<=lat<=55.8271): return lon,lat
    x,y=lon-105,lat-35
    dy=-100+2*x+3*y+.2*y*y+.1*x*y+.2*math.sqrt(abs(x))
    dx=300+x+2*y+.1*x*x+.1*x*y+.1*math.sqrt(abs(x))
    t=(20*math.sin(6*x*math.pi)+20*math.sin(2*x*math.pi))*2/3
    dy+=t+(20*math.sin(y*math.pi)+40*math.sin(y/3*math.pi))*2/3+(160*math.sin(y/12*math.pi)+320*math.sin(y/30*math.pi))*2/3
    dx+=t+(20*math.sin(x*math.pi)+40*math.sin(x/3*math.pi))*2/3+(150*math.sin(x/12*math.pi)+300*math.sin(x/30*math.pi))*2/3
    r=math.radians(lat);m=1-.00669342162296594323*math.sin(r)**2;a=6378245
    return lon+dx*180/(a/math.sqrt(m)*math.cos(r)*math.pi),lat+dy*180/(a*(1-.00669342162296594323)/m**1.5*math.pi)
def bd09_to_wgs84(lon,lat):
    x,y=lon-.0065,lat-.006;k=math.pi*3000/180
    z=math.hypot(x,y)-.00002*math.sin(y*k);t=math.atan2(y,x)-.000003*math.cos(x*k)
    gx,gy=z*math.cos(t),z*math.sin(t);x,y=gx,gy
    for _ in range(10):
        tx,ty=wgs84_to_gcj02(x,y);x-=tx-gx;y-=ty-gy
    return x,y
def distance_m(lon1,lat1,lon2,lat2):
    return abs(Geod(ellps='WGS84').inv(*bd09_to_wgs84(lon1,lat1),*bd09_to_wgs84(lon2,lat2))[2])
