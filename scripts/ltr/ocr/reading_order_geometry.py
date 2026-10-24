"""stdlib-only port of the legacy word-box vertical geometry candidate algorithm.

No OCR text is accepted or returned. Geometry indicates a machine proposal only.
"""
from __future__ import annotations

import math

METHOD = "vertical-word-bands-v2"
MAX_LINES = 20_000
MAX_WORDS = 100_000
MAX_DIMENSION = 20_000

class GeometryError(ValueError):
    def __init__(self, code="geometry_invalid"):
        self.code = code
        super().__init__(code)

def _need(condition, code="geometry_invalid"):
    if not condition:
        raise GeometryError(code)

def _num(value):
    return type(value) in (int, float) and math.isfinite(value)

def _integer(value, minimum=1):
    return type(value) is int and value >= minimum

def _bounds(words):
    if not words:
        return None
    return (min(w["x"] for w in words), min(w["y"] for w in words),
            max(w["x"] + w["w"] for w in words),
            max(w["y"] + w["h"] for w in words))

def _percentile80(values):
    """Match numpy.percentile(..., 80) linear interpolation without numpy."""
    _need(bool(values))
    ordered=sorted(values)
    pos=(len(ordered)-1)*0.8
    lo=int(math.floor(pos)); hi=int(math.ceil(pos))
    return float(ordered[lo] + (ordered[hi]-ordered[lo])*(pos-lo))

def _validate(lines, width, height):
    _need(_integer(width) and width<=MAX_DIMENSION and _integer(height) and height<=MAX_DIMENSION,
          "geometry_limits_exceeded")
    _need(type(lines) is list and len(lines)<=MAX_LINES, "geometry_limits_exceeded")
    result=[]; total=0
    for line in lines:
        _need(type(line) is dict and type(line.get("words")) is list, "geometry_invalid")
        words=[]
        for word in line["words"]:
            total+=1
            _need(total<=MAX_WORDS, "geometry_limits_exceeded")
            _need(type(word) is dict and all(k in word for k in ("x","y","w","h")), "geometry_invalid")
            x,y,w,h=(word[k] for k in ("x","y","w","h"))
            _need(all(_num(v) for v in (x,y,w,h)) and w>0 and h>0 and x>=0 and y>=0 and
                  x+w<=width and y+h<=height, "geometry_invalid")
            words.append({"x":x,"y":y,"w":w,"h":h})
        result.append({"words":words})
    return result,total

def word_order(lines, width, height):
    """Return a geometry-only ordering preserving every source word exactly once."""
    lines,total=_validate(lines,width,height)
    words=[(li,wi,w) for li,line in enumerate(lines) for wi,w in enumerate(line["words"])]
    boxes=[_bounds(line["words"]) for line in lines]
    tall=[b for b in boxes if b and b[3]-b[1]>max(3*(b[2]-b[0]),height*.1)]
    if len(tall)<4:
        refs=[[li,wi] for li,line in enumerate(lines) for wi,_ in enumerate(line["words"])]
        return {"layout":"unclassified","raw_line_count":len(lines),"raw_word_count":total,
                "segments":[],"word_order":refs,"bands":[],"coverage_exact_once":True,
                "raw_backward_band_steps":0,"new_backward_band_steps":0,"main":[],"ruby":[],"other":[]}
    typical=_percentile80([b[2]-b[0] for b in tall])
    _need(_num(typical) and typical>0,"geometry_invalid")
    occupancy=[0]*(height+1)
    for li,wi,w in words:
        if typical*.25<=w["w"]<=typical*1.6 and w["h"]>=2:
            start=max(0,int(w["y"])); end=min(height,int(w["y"]+w["h"])+1)
            for y in range(start,end): occupancy[y]+=1
    peak=max(occupancy,default=0)
    active=[i for i,v in enumerate(occupancy) if v>=max(2,peak*.06)]
    bands=[]
    for y in active:
        if not bands or y-bands[-1][1]>max(6,height*.014): bands.append([int(y),int(y)])
        else: bands[-1][1]=int(y)
    bands=[b for b in bands if b[1]-b[0]>=height*.09]
    if not bands: bands=[[0,height]]
    def band(word):
        return min(range(len(bands)),key=lambda k:abs(word["y"]+word["h"]/2-sum(bands[k])/2))
    groups={}
    for li,wi,w in words: groups.setdefault((band(w),li),[]).append((wi,w))
    segments=[]
    for (k,li),items in groups.items():
        box=_bounds([w for _,w in items]); bw=box[2]-box[0]; bh=box[3]-box[1]
        role="main" if bw>=typical*.65 and bh>bw*1.2 else ("ruby" if bw<typical*.65 and bh>bw*1.2 else "other")
        refs=[[li,wi] for wi,w in sorted(items,key=lambda pair:pair[1]["y"])]
        segments.append({"band":k,"source_line":li,"box":list(box),"role":role,"word_refs":refs})
    for k in range(len(bands)):
        centers=[]
        for seg in sorted((s for s in segments if s["band"]==k),key=lambda s:-(s["box"][0]+s["box"][2])/2):
            cx=(seg["box"][0]+seg["box"][2])/2
            col=next((i for i,cx0 in enumerate(centers) if abs(cx-cx0)<=typical*.28),None)
            if col is None: centers.append(cx); col=len(centers)-1
            seg["column"]=col
    segments.sort(key=lambda s:(s["band"],s["column"],s["box"][1]))
    refs=[r for seg in segments for r in seg["word_refs"]]
    _need(sorted(map(tuple,refs))==[(li,wi) for li,line in enumerate(lines) for wi,_ in enumerate(line["words"])],
          "geometry_word_coverage")
    rawbands=[band(w) for _,_,w in words]
    newbands=[band(lines[li]["words"][wi]) for li,wi in refs]
    return {"layout":"vertical_multi_band" if len(bands)>1 else "vertical_single_band",
            "raw_line_count":len(lines),"raw_word_count":total,"word_order":refs,"segments":segments,
            "bands":bands,"coverage_exact_once":True,
            "raw_backward_band_steps":sum(a>b for a,b in zip(rawbands,rawbands[1:])),
            "new_backward_band_steps":sum(a>b for a,b in zip(newbands,newbands[1:])),
            "main":[i for i,s in enumerate(segments) if s["role"]=="main"],
            "ruby":[i for i,s in enumerate(segments) if s["role"]=="ruby"],
            "other":[i for i,s in enumerate(segments) if s["role"]=="other"]}
