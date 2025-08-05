#!/usr/bin/env python3
# Sonix UVC Control GUI — v3 (Linux)
# Live preview + V4L2 sliders + Sonix vendor controls via SONiX_UVC_TestAP
# Default tool path: ~/C1_SONIX_Test_AP/SONiX_UVC_TestAP

import os, re, glob, subprocess, threading, time
from typing import List, Tuple, Optional

import cv2
import numpy as np
from PIL import Image, ImageTk

import tkinter as tk
from tkinter import ttk, filedialog

# ---------- helpers ----------

DEFAULT_TOOL = os.path.expanduser("~/C1_SONIX_Test_AP/SONiX_UVC_TestAP")

def run_tool(tool: str, args: List[str], timeout=8) -> Tuple[int, str, str]:
    try:
        p = subprocess.run([tool] + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           text=True, timeout=timeout, check=False)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", f"Tool not found: {tool}"
    except subprocess.TimeoutExpired:
        return 124, "", "Timed out"

def list_cameras() -> List[Tuple[str,str]]:
    """Return [(path, label), ...] for /dev/video*; label tries to include card name via v4l2-ctl."""
    devs = sorted(glob.glob("/dev/video*"))
    out = []
    # Try to get names with v4l2-ctl --list-devices
    names = {}
    try:
        q = subprocess.run(["v4l2-ctl","--list-devices"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False)
        block = None
        for line in q.stdout.splitlines():
            if line.strip() and not line.startswith("\t"):
                block = line.strip()
            else:
                m = re.search(r"(/dev/video\d+)", line)
                if m and block:
                    names[m.group(1)] = block
    except Exception:
        pass
    for d in devs:
        label = f"{d}"
        if d in names:
            label = f"{names[d]} — {d}"
        out.append((d, label))
    return out

def parse_osd_get_enable(out: str) -> Tuple[Optional[int], Optional[int]]:
    m1 = re.search(r'OSD\s+Enable\s+Line\s*=\s*(\d+)', out)
    m2 = re.search(r'OSD\s+Enable\s+Block\s*=\s*(\d+)', out)
    return (int(m1.group(1)) if m1 else None, int(m2.group(1)) if m2 else None)

# ---------- GUI ----------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Sonix UVC Control GUI v3")
        self.geometry("1400x900")
        self.tool = tk.StringVar(value=DEFAULT_TOOL)
        self.cap = None
        self.preview_on = False
        self.preview_box = (640, 360)

        # resolution/fps
        self.req_w = tk.IntVar(value=1280)
        self.req_h = tk.IntVar(value=720)
        self.req_fps = tk.IntVar(value=30)

        self._build_ui()
        self._refresh_cameras()
        self.after(300, self._start_preview)  # let layout settle

    # ----- UI scaffold -----
    def _build_ui(self):
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=2)
        self.grid_columnconfigure(1, weight=1)

        # LEFT
        left = ttk.Frame(self)
        left.grid(row=0, column=0, sticky="nsew")
        left.grid_rowconfigure(3, weight=1)
        left.grid_columnconfigure(0, weight=1)

        top = ttk.Frame(left); top.grid(row=0, column=0, sticky="ew", padx=6, pady=4)
        ttk.Label(top, text="Tool:").grid(row=0, column=0, sticky="w")
        tk.Entry(top, textvariable=self.tool, width=48).grid(row=0, column=1, sticky="w", padx=4)
        ttk.Button(top, text="Browse…", command=self._pick_tool).grid(row=0, column=2, padx=2)

        ttk.Label(top, text="Device:").grid(row=0, column=3, sticky="w", padx=(12,0))
        self.dev_var = tk.StringVar(value="/dev/video0")
        self.dev_combo = ttk.Combobox(top, textvariable=self.dev_var, width=34, state="readonly")
        self.dev_combo.grid(row=0, column=4, sticky="w", padx=4)
        ttk.Button(top, text="Refresh", command=self._refresh_cameras).grid(row=0, column=5, padx=2)
        ttk.Button(top, text="Map XU", command=self.map_xu).grid(row=0, column=6, padx=6)

        res = ttk.Frame(left); res.grid(row=1, column=0, sticky="ew", padx=6, pady=(0,4))
        ttk.Label(res, text="W").grid(row=0, column=0); tk.Spinbox(res, from_=160, to=3840, textvariable=self.req_w, width=6).grid(row=0, column=1)
        ttk.Label(res, text="H").grid(row=0, column=2); tk.Spinbox(res, from_=120, to=2160, textvariable=self.req_h, width=6).grid(row=0, column=3)
        ttk.Label(res, text="FPS").grid(row=0, column=4); tk.Spinbox(res, from_=1, to=120, textvariable=self.req_fps, width=6).grid(row=0, column=5)
        ttk.Button(res, text="Apply", command=self._apply_resolution).grid(row=0, column=6, padx=6)
        ttk.Button(res, text="Restart Preview", command=self._start_preview).grid(row=0, column=7, padx=6)

        self.canvas = tk.Label(left, bg="black")
        self.canvas.grid(row=2, column=0, sticky="nsew", padx=6, pady=4)
        self.canvas.bind("<Configure>", self._on_canvas_resize)

        std = ttk.LabelFrame(left, text="Standard V4L2 controls (best effort via OpenCV)")
        std.grid(row=3, column=0, sticky="ew", padx=6, pady=4)
        std.grid_columnconfigure(1, weight=1)
        self._add_slider(std, "Brightness", cv2.CAP_PROP_BRIGHTNESS, 0, 255)
        self._add_slider(std, "Contrast",   cv2.CAP_PROP_CONTRAST,   0, 255)
        self._add_slider(std, "Saturation", cv2.CAP_PROP_SATURATION, 0, 255)
        self._add_slider(std, "Gain",       cv2.CAP_PROP_GAIN,       0, 255)
        self._add_slider(std, "Exposure",   cv2.CAP_PROP_EXPOSURE,  -13, -1)

        # RIGHT tabs
        right = ttk.Notebook(self); right.grid(row=0, column=1, sticky="nsew")

        self._build_tab_osd(right)
        self._build_tab_rtc(right)
        self._build_tab_motion(right)
        self._build_tab_h264(right)
        self._build_tab_misc(right)

        # Log
        tlog = ttk.Frame(right); right.add(tlog, text="Log")
        tlog.grid_rowconfigure(0, weight=1); tlog.grid_columnconfigure(0, weight=1)
        self.log = tk.Text(tlog); self.log.grid(row=0, column=0, sticky="nsew")

    def _pick_tool(self):
        p = filedialog.askopenfilename(title="Select SONiX_UVC_TestAP", initialdir=os.path.expanduser("~"))
        if p: self.tool.set(p)

    def _refresh_cameras(self):
        cams = list_cameras()
        items = [label for _,label in cams] or ["/dev/video0"]
        # Map label->path
        self._label_to_path = {label:path for path,label in cams}
        self.dev_combo["values"] = items
        # Keep selection stable
        cur = self.dev_combo.get()
        if cur and cur in self._label_to_path:
            self.dev_var.set(cur)
        else:
            self.dev_var.set(items[0])

    def _current_device(self) -> str:
        label = self.dev_var.get()
        return self._label_to_path.get(label, label)  # fall back to raw path

    # ----- sliders -----
    def _add_slider(self, parent, name, prop, frm, to):
        r = len(parent.grid_slaves()) // 2
        ttk.Label(parent, text=name).grid(row=r, column=0, sticky="w", padx=4, pady=2)
        s = ttk.Scale(parent, from_=frm, to=to, orient="horizontal")
        s.grid(row=r, column=1, sticky="ew", padx=4, pady=2)
        def on_rel(e):
            try:
                if self.cap: self.cap.set(prop, s.get())
            except Exception: pass
        s.bind("<ButtonRelease-1>", on_rel)

    # ----- preview -----
    def _on_canvas_resize(self, e):
        self.preview_box = (max(64, e.width), max(64, e.height))

    def _start_preview(self):
        # restart
        try:
            if self.cap:
                try: self.cap.release()
                except: pass
            idx = self._dev_index(self._current_device())
            self.cap = cv2.VideoCapture(idx, cv2.CAP_V4L2)
            self._apply_resolution()
            self.preview_on = True
            threading.Thread(target=self._loop, daemon=True).start()
            self._log(f"[preview] opened {self._current_device()}")
        except Exception as e:
            self._log(f"[preview] failed: {e}")

    def _dev_index(self, path: str) -> int:
        m = re.search(r'/dev/video(\d+)', path)
        return int(m.group(1)) if m else 0

    def _apply_resolution(self):
        try:
            if not self.cap: return
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH,  int(self.req_w.get()))
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, int(self.req_h.get()))
            self.cap.set(cv2.CAP_PROP_FPS,          int(self.req_fps.get()))
            self._log(f"[preview] request {self.req_w.get()}x{self.req_h.get()}@{self.req_fps.get()}")
        except Exception as e:
            self._log(f"[preview] res set error: {e}")

    def _fit(self, frame, box_w, box_h):
        h,w = frame.shape[:2]
        if w == 0 or h == 0: return frame
        scale = min(box_w/w, box_h/h)
        if scale <= 0: scale = 1.0
        return cv2.resize(frame, (int(w*scale), int(h*scale)))

    def _loop(self):
        while self.preview_on:
            ok, frame = (self.cap.read() if self.cap else (False, None))
            if not ok:
                time.sleep(0.05); continue
            disp = self._fit(frame, *self.preview_box)
            rgb = cv2.cvtColor(disp, cv2.COLOR_BGR2RGB)
            imgtk = ImageTk.PhotoImage(Image.fromarray(rgb))
            self.canvas.imgtk = imgtk
            self.canvas.configure(image=imgtk)

    # ----- log -----
    def _log(self, s: str):
        try:
            self.log.insert("end", s+"\n"); self.log.see("end")
        except Exception:
            print(s)

    # ----- vendor: Map XU -----
    def map_xu(self):
        rc,out,err = run_tool(self.tool.get(), ["-a", self._current_device()], timeout=10)
        self._log(f"[map_xu rc={rc}] {out or err}")

    # ----- vendor: OSD -----
    def _build_tab_osd(self, nb: ttk.Notebook):
        t = ttk.Frame(nb); nb.add(t, text="OSD")
        t.grid_columnconfigure(1, weight=1); r=0

        ttk.Label(t,text="OSD Show (Line/Block)").grid(row=r,column=0,sticky="w",padx=4,pady=2)
        self.oe_line=tk.IntVar(value=1); self.oe_block=tk.IntVar(value=1)
        tk.Spinbox(t,from_=0,to=1,textvariable=self.oe_line,width=4).grid(row=r,column=1,sticky="w")
        tk.Spinbox(t,from_=0,to=1,textvariable=self.oe_block,width=4).grid(row=r,column=2,sticky="w")
        ttk.Button(t,text="GET",command=self.osd_get_oe).grid(row=r,column=3)
        ttk.Button(t,text="SET",command=self.osd_set_oe).grid(row=r,column=4); r+=1

        ttk.Label(t,text="Timer enable").grid(row=r,column=0,sticky="w",padx=4,pady=2)
        self.timer=tk.IntVar(value=0)
        tk.Spinbox(t,from_=0,to=1,textvariable=self.timer,width=6).grid(row=r,column=1,sticky="w")
        ttk.Button(t,text="SET",command=self.osd_set_timer).grid(row=r,column=3); r+=1

        ttk.Label(t,text="OSD Size (Line/Block 0..4)").grid(row=r,column=0,sticky="w",padx=4,pady=2)
        self.os_line=tk.IntVar(value=2); self.os_block=tk.IntVar(value=2)
        tk.Spinbox(t,from_=0,to=4,textvariable=self.os_line,width=4).grid(row=r,column=1,sticky="w")
        tk.Spinbox(t,from_=0,to=4,textvariable=self.os_block,width=4).grid(row=r,column=2,sticky="w")
        ttk.Button(t,text="GET",command=self.osd_get_os).grid(row=r,column=3)
        ttk.Button(t,text="SET",command=self.osd_set_os).grid(row=r,column=4); r+=1

        ttk.Label(t,text="AutoScale (Line/Block)").grid(row=r,column=0,sticky="w",padx=4,pady=2)
        self.oas_line=tk.IntVar(value=0); self.oas_block=tk.IntVar(value=0)
        tk.Spinbox(t,from_=0,to=1,textvariable=self.oas_line,width=4).grid(row=r,column=1,sticky="w")
        tk.Spinbox(t,from_=0,to=1,textvariable=self.oas_block,width=4).grid(row=r,column=2,sticky="w")
        ttk.Button(t,text="GET",command=self.osd_get_oas).grid(row=r,column=3)
        ttk.Button(t,text="SET",command=self.osd_set_oas).grid(row=r,column=4); r+=1

        ttk.Label(t,text="Colors (Font/Border 0..4)").grid(row=r,column=0,sticky="w",padx=4,pady=2)
        self.oc_font=tk.IntVar(value=4); self.oc_border=tk.IntVar(value=0)
        tk.Spinbox(t,from_=0,to=4,textvariable=self.oc_font,width=4).grid(row=r,column=1,sticky="w")
        tk.Spinbox(t,from_=0,to=4,textvariable=self.oc_border,width=4).grid(row=r,column=2,sticky="w")
        ttk.Button(t,text="GET",command=self.osd_get_oc).grid(row=r,column=3)
        ttk.Button(t,text="SET",command=self.osd_set_oc).grid(row=r,column=4); r+=1

        ttk.Label(t,text="Start (Type/Row/Col) unit=16").grid(row=r,column=0,sticky="w",padx=4,pady=2)
        self.osp_type=tk.IntVar(value=1); self.osp_row=tk.IntVar(value=0); self.osp_col=tk.IntVar(value=0)
        tk.Spinbox(t,from_=0,to=2,textvariable=self.osp_type,width=4).grid(row=r,column=1,sticky="w")
        tk.Spinbox(t,from_=0,to=400,textvariable=self.osp_row,width=6).grid(row=r,column=2,sticky="w")
        tk.Spinbox(t,from_=0,to=400,textvariable=self.osp_col,width=6).grid(row=r,column=3,sticky="w")
        ttk.Button(t,text="GET",command=self.osd_get_osp).grid(row=r,column=4)
        ttk.Button(t,text="SET",command=self.osd_set_osp).grid(row=r,column=5); r+=1

        ttk.Label(t,text="Multistream size (S0/S1/S2 0..4)").grid(row=r,column=0,sticky="w",padx=4,pady=2)
        self.oms0=tk.IntVar(value=2); self.oms1=tk.IntVar(value=2); self.oms2=tk.IntVar(value=2)
        tk.Spinbox(t,from_=0,to=4,textvariable=self.oms0,width=4).grid(row=r,column=1,sticky="w")
        tk.Spinbox(t,from_=0,to=4,textvariable=self.oms1,width=4).grid(row=r,column=2,sticky="w")
        tk.Spinbox(t,from_=0,to=4,textvariable=self.oms2,width=4).grid(row=r,column=3,sticky="w")
        ttk.Button(t,text="GET",command=self.osd_get_oms).grid(row=r,column=4)
        ttk.Button(t,text="SET",command=self.osd_set_oms).grid(row=r,column=5); r+=1

        ttk.Label(t,text="2nd String (Group 0..2)").grid(row=r,column=0,sticky="w",padx=4,pady=2)
        self.ostr_group=tk.IntVar(value=0); self.ostr_text=tk.Entry(t,width=28)
        tk.Spinbox(t,from_=0,to=2,textvariable=self.ostr_group,width=4).grid(row=r,column=1,sticky="w")
        self.ostr_text.grid(row=r,column=2,columnspan=3,sticky="ew",padx=4)
        ttk.Button(t,text="GET",command=self.osd_get_ostr).grid(row=r,column=5)
        ttk.Button(t,text="SET",command=self.osd_set_ostr).grid(row=r,column=6)

    # ----- vendor: OSD ops -----
    def osd_get_oe(self):
        rc,out,err = run_tool(self.tool.get(), ["--xuget-oe", self._current_device()])
        self._log(out or err)
        line,block = parse_osd_get_enable(out)
        if line is not None: self.oe_line.set(line)
        if block is not None: self.oe_block.set(block)

    def osd_set_oe(self):
        arg = f"{self.oe_line.get()} {self.oe_block.get()}"
        rc,out,err = run_tool(self.tool.get(), ["--xuset-oe", arg, self._current_device()])
        self._log(out or err)

    def osd_set_timer(self):
        rc,out,err = run_tool(self.tool.get(), ["--xuset-timer", str(self.timer.get()), self._current_device()])
        self._log(out or err)

    def osd_get_os(self):
        rc,out,err = run_tool(self.tool.get(), ["--xuget-os", self._current_device()])
        self._log(out or err)

    def osd_set_os(self):
        arg = f"{self.os_line.get()} {self.os_block.get()}"
        rc,out,err = run_tool(self.tool.get(), ["--xuset-os", arg, self._current_device()])
        self._log(out or err)

    def osd_get_oas(self):
        rc,out,err = run_tool(self.tool.get(), ["--xuget-oas", self._current_device()])
        self._log(out or err)

    def osd_set_oas(self):
        arg = f"{self.oas_line.get()} {self.oas_block.get()}"
        rc,out,err = run_tool(self.tool.get(), ["--xuset-oas", arg, self._current_device()])
        self._log(out or err)

    def osd_get_oc(self):
        rc,out,err = run_tool(self.tool.get(), ["--xuget-oc", self._current_device()])
        self._log(out or err)

    def osd_set_oc(self):
        arg = f"{self.oc_font.get()} {self.oc_border.get()}"
        rc,out,err = run_tool(self.tool.get(), ["--xuset-oc", arg, self._current_device()])
        self._log(out or err)

    def osd_get_osp(self):
        rc,out,err = run_tool(self.tool.get(), ["--xuget-osp", self._current_device()])
        self._log(out or err)

    def osd_set_osp(self):
        arg = f"{self.osp_type.get()} {self.osp_row.get()} {self.osp_col.get()}"
        rc,out,err = run_tool(self.tool.get(), ["--xuset-osp", arg, self._current_device()])
        self._log(out or err)

    def osd_get_oms(self):
        rc,out,err = run_tool(self.tool.get(), ["--xuget-oms", self._current_device()])
        self._log(out or err)

    def osd_set_oms(self):
        arg = f"{self.oms0.get()} {self.oms1.get()} {self.oms2.get()}"
        rc,out,err = run_tool(self.tool.get(), ["--xuset-oms", arg, self._current_device()])
        self._log(out or err)

    def osd_get_ostr(self):
        rc,out,err = run_tool(self.tool.get(), ["--xuget-ostr", str(self.ostr_group.get()), self._current_device()])
        self._log(out or err)

    def osd_set_ostr(self):
        arg = f"{self.ostr_group.get()} '{self.ostr_text.get()}'"
        rc,out,err = run_tool(self.tool.get(), ["--xuset-ostr", arg, self._current_device()])
        self._log(out or err)

    # ----- RTC tab -----
    def _build_tab_rtc(self, nb):
        t = ttk.Frame(nb); nb.add(t, text="RTC"); t.grid_columnconfigure(1,weight=1); r=0
        ttk.Label(t,text="Set RTC (Y M D h m s)").grid(row=r,column=0,sticky="w",padx=4,pady=2)
        self.rY=tk.IntVar(value=2025); self.rM=tk.IntVar(value=1); self.rD=tk.IntVar(value=1)
        self.rH=tk.IntVar(value=0); self.rMin=tk.IntVar(value=0); self.rS=tk.IntVar(value=0)
        for i,(v,maxv,w) in enumerate([(self.rY,9999,6),(self.rM,12,4),(self.rD,31,4),(self.rH,23,4),(self.rMin,59,4),(self.rS,59,4)]):
            tk.Spinbox(t,from_=0,to=maxv,textvariable=v,width=w).grid(row=r,column=1+i,sticky="w")
        ttk.Button(t,text="SET",command=self.rtc_set).grid(row=r,column=7,padx=2); r+=1
        ttk.Button(t,text="GET",command=self.rtc_get).grid(row=r,column=0,padx=2,pady=4)
        self.rtc_label=ttk.Label(t,text="RTC: --"); self.rtc_label.grid(row=r,column=1,columnspan=4,sticky="w")

    def rtc_set(self):
        args = f"{self.rY.get()} {self.rM.get()} {self.rD.get()} {self.rH.get()} {self.rMin.get()} {self.rS.get()}"
        rc,out,err = run_tool(self.tool.get(), ["--xuset-rtc", args, self._current_device()])
        self._log(out or err)

    def rtc_get(self):
        rc,out,err = run_tool(self.tool.get(), ["--xuget-rtc", self._current_device()])
        self._log(out or err)
        m = re.search(r'(\d{4})/(\d{1,2})/(\d{1,2}).*?(\d{1,2}):(\d{1,2}):(\d{1,2})', out)
        if m:
            self.rtc_label.config(text=f"RTC: {m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d} {int(m.group(4)):02d}:{int(m.group(5)):02d}:{int(m.group(6)):02d}")

    # ----- Motion tab -----
    def _build_tab_motion(self, nb):
        t = ttk.Frame(nb); nb.add(t, text="Motion"); t.grid_columnconfigure(1,weight=1); r=0
        self.md_en=tk.IntVar(value=0); self.md_th=tk.IntVar(value=1000)
        ttk.Label(t,text="Enable (0/1)").grid(row=r,column=0,sticky="w",padx=4,pady=2)
        tk.Spinbox(t,from_=0,to=1,textvariable=self.md_en,width=4).grid(row=r,column=1,sticky="w")
        ttk.Button(t,text="SET",command=self.md_set_en).grid(row=r,column=2); ttk.Button(t,text="GET",command=self.md_get_en).grid(row=r,column=3); r+=1
        ttk.Label(t,text="Threshold 0..65535").grid(row=r,column=0,sticky="w",padx=4,pady=2)
        tk.Spinbox(t,from_=0,to=65535,textvariable=self.md_th,width=8).grid(row=r,column=1,sticky="w")
        ttk.Button(t,text="SET",command=self.md_set_th).grid(row=r,column=2); ttk.Button(t,text="GET",command=self.md_get_th).grid(row=r,column=3); r+=1
        ttk.Label(t,text="Mask (24 ints)").grid(row=r,column=0,sticky="w",padx=4,pady=2)
        self.md_mask=tk.Entry(t,width=50); self.md_mask.grid(row=r,column=1,columnspan=4,sticky="ew")
        ttk.Button(t,text="SET",command=self.md_set_mask).grid(row=r,column=5); ttk.Button(t,text="GET",command=self.md_get_mask).grid(row=r,column=6); r+=1
        ttk.Label(t,text="Result (24 ints)").grid(row=r,column=0,sticky="w",padx=4,pady=2)
        self.md_res=tk.Entry(t,width=50); self.md_res.grid(row=r,column=1,columnspan=4,sticky="ew")
        ttk.Button(t,text="GET",command=self.md_get_res).grid(row=r,column=5)

    def md_set_en(self):
        rc,out,err = run_tool(self.tool.get(), ["--xuset-mde", str(self.md_en.get()), self._current_device()])
        self._log(out or err)
    def md_get_en(self):
        rc,out,err = run_tool(self.tool.get(), ["--xuget-mde", self._current_device()])
        self._log(out or err)
    def md_set_th(self):
        rc,out,err = run_tool(self.tool.get(), ["--xuset-mdt", str(self.md_th.get()), self._current_device()])
        self._log(out or err)
    def md_get_th(self):
        rc,out,err = run_tool(self.tool.get(), ["--xuget-mdt", self._current_device()])
        self._log(out or err)
    def md_set_mask(self):
        arg = self.md_mask.get().strip()
        rc,out,err = run_tool(self.tool.get(), ["--xuset-mdm", arg, self._current_device()], timeout=10)
        self._log(out or err)
    def md_get_mask(self):
        rc,out,err = run_tool(self.tool.get(), ["--xuget-mdm", self._current_device()], timeout=10)
        self._log(out or err)
    def md_get_res(self):
        rc,out,err = run_tool(self.tool.get(), ["--xuget-mdr", self._current_device()], timeout=10)
        self._log(out or err)
        vals = re.findall(r'\b\d+\b', out)
        try:
            self.md_res.delete(0,"end"); self.md_res.insert(0, " ".join(vals[:24]))
        except Exception: pass

    # ----- H264/MJPG tab -----
    def _build_tab_h264(self, nb):
        t = ttk.Frame(nb); nb.add(t, text="H264/MJPG"); t.grid_columnconfigure(1,weight=1); r=0
        self.mjpg_bps=tk.IntVar(value=1_000_000)
        ttk.Label(t,text="MJPG bitrate (bps)").grid(row=r,column=0,sticky="w",padx=4,pady=2)
        tk.Entry(t,textvariable=self.mjpg_bps,width=14).grid(row=r,column=1,sticky="w")
        ttk.Button(t,text="SET",command=self.mjpg_set).grid(row=r,column=2); ttk.Button(t,text="GET",command=self.mjpg_get).grid(row=r,column=3); r+=1
        self.gop=tk.IntVar(value=30); self.cvm=tk.IntVar(value=1); self.iframe=tk.IntVar(value=0); self.sei=tk.IntVar(value=1)
        ttk.Label(t,text="GOP").grid(row=r,column=0,sticky="w"); tk.Spinbox(t,from_=1,to=4095,textvariable=self.gop,width=8).grid(row=r,column=1,sticky="w")
        ttk.Button(t,text="SET",command=self.h264_set_gop).grid(row=r,column=2); ttk.Button(t,text="GET",command=self.h264_get_gop).grid(row=r,column=3); r+=1
        ttk.Label(t,text="Mode 1=CBR 2=VBR").grid(row=r,column=0,sticky="w"); tk.Spinbox(t,from_=1,to=2,textvariable=self.cvm,width=6).grid(row=r,column=1,sticky="w")
        ttk.Button(t,text="SET",command=self.h264_set_cvm).grid(row=r,column=2); ttk.Button(t,text="GET",command=self.h264_get_cvm).grid(row=r,column=3); r+=1
        ttk.Label(t,text="I-Frame reset per n (0=never)").grid(row=r,column=0,sticky="w"); tk.Spinbox(t,from_=0,to=10000,textvariable=self.iframe,width=10).grid(row=r,column=1,sticky="w")
        ttk.Button(t,text="SET",command=self.h264_set_if).grid(row=r,column=2); r+=1
        ttk.Label(t,text="SEI enable 1/0").grid(row=r,column=0,sticky="w"); tk.Spinbox(t,from_=0,to=1,textvariable=self.sei,width=6).grid(row=r,column=1,sticky="w")
        ttk.Button(t,text="SET",command=self.h264_set_sei).grid(row=r,column=2)

    def mjpg_set(self):
        rc,out,err = run_tool(self.tool.get(), ["--xuset-mjb", str(self.mjpg_bps.get()), self._current_device()])
        self._log(out or err)
    def mjpg_get(self):
        rc,out,err = run_tool(self.tool.get(), ["--xuget-mjb", self._current_device()])
        self._log(out or err)
    def h264_set_gop(self):
        rc,out,err = run_tool(self.tool.get(), ["--xuset-gop", str(self.gop.get()), self._current_device()])
        self._log(out or err)
    def h264_get_gop(self):
        rc,out,err = run_tool(self.tool.get(), ["--xuget-gop", self._current_device()])
        self._log(out or err)
    def h264_set_cvm(self):
        rc,out,err = run_tool(self.tool.get(), ["--xuset-cvm", str(self.cvm.get()), self._current_device()])
        self._log(out or err)
    def h264_get_cvm(self):
        rc,out,err = run_tool(self.tool.get(), ["--xuget-cvm", self._current_device()])
        self._log(out or err)
    def h264_set_if(self):
        rc,out,err = run_tool(self.tool.get(), ["--xuset-if", str(self.iframe.get()), self._current_device()])
        self._log(out or err)
    def h264_set_sei(self):
        rc,out,err = run_tool(self.tool.get(), ["--xuset-sei", self._current_device()])
        self._log(out or err)

    # ----- Misc tab -----
    def _build_tab_misc(self, nb):
        t = ttk.Frame(nb); nb.add(t, text="Image/GPIO/Misc"); t.grid_columnconfigure(1,weight=1); r=0
        self.mirror=tk.IntVar(value=0); self.flip=tk.IntVar(value=0)
        ttk.Label(t,text="Mirror 0/1").grid(row=r,column=0,sticky="w"); tk.Spinbox(t,from_=0,to=1,textvariable=self.mirror,width=6).grid(row=r,column=1,sticky="w")
        ttk.Button(t,text="SET",command=self.set_mirror).grid(row=r,column=2); ttk.Button(t,text="GET",command=self.get_mirror).grid(row=r,column=3); r+=1
        ttk.Label(t,text="Flip 0/1").grid(row=r,column=0,sticky="w"); tk.Spinbox(t,from_=0,to=1,textvariable=self.flip,width=6).grid(row=r,column=1,sticky="w")
        ttk.Button(t,text="SET",command=self.set_flip).grid(row=r,column=2); ttk.Button(t,text="GET",command=self.get_flip).grid(row=r,column=3); r+=1
        ttk.Label(t,text="GPIO (hex enable/out)").grid(row=r,column=0,sticky="w"); self.gpio=tk.StringVar(value="00000000")
        tk.Entry(t,textvariable=self.gpio,width=16).grid(row=r,column=1,sticky="w"); ttk.Button(t,text="SET",command=self.set_gpio).grid(row=r,column=2)
        ttk.Button(t,text="GET",command=self.get_gpio).grid(row=r,column=3); r+=1
        self.fde1=tk.IntVar(value=0); self.fde2=tk.IntVar(value=0); self.fdc1=tk.IntVar(value=0); self.fdc2=tk.IntVar(value=0)
        ttk.Label(t,text="Frame drop enable s1/s2").grid(row=r,column=0,sticky="w")
        tk.Spinbox(t,from_=0,to=1,textvariable=self.fde1,width=6).grid(row=r,column=1,sticky="w")
        tk.Spinbox(t,from_=0,to=1,textvariable=self.fde2,width=6).grid(row=r,column=2,sticky="w")
        ttk.Button(t,text="SET",command=self.set_fde).grid(row=r,column=3); r+=1
        ttk.Label(t,text="Frame drop count s1/s2").grid(row=r,column=0,sticky="w")
        tk.Spinbox(t,from_=0,to=65535,textvariable=self.fdc1,width=8).grid(row=r,column=1,sticky="w")
        tk.Spinbox(t,from_=0,to=65535,textvariable=self.fdc2,width=8).grid(row=r,column=2,sticky="w")
        ttk.Button(t,text="SET",command=self.set_fdc).grid(row=r,column=3)

    def set_mirror(self):
        rc,out,err = run_tool(self.tool.get(), ["--xuset-mir", self._current_device()])
        self._log(out or err)
    def get_mirror(self):
        rc,out,err = run_tool(self.tool.get(), ["--xuget-mir", self._current_device()])
        self._log(out or err)
    def set_flip(self):
        rc,out,err = run_tool(self.tool.get(), ["--xuset-flip", self._current_device()])
        self._log(out or err)
    def get_flip(self):
        rc,out,err = run_tool(self.tool.get(), ["--xuget-flip", self._current_device()])
        self._log(out or err)
    def set_gpio(self):
        rc,out,err = run_tool(self.tool.get(), ["--xuset-gpio", self.gpio.get(), self._current_device()])
        self._log(out or err)
    def get_gpio(self):
        rc,out,err = run_tool(self.tool.get(), ["--xuget-gpio", self._current_device()])
        self._log(out or err)
    def set_fde(self):
        arg=f"{self.fde1.get()} {self.fde2.get()}"; rc,out,err = run_tool(self.tool.get(), ["--xuset-fde", arg, self._current_device()]); self._log(out or err)
    def set_fdc(self):
        arg=f"{self.fdc1.get()} {self.fdc2.get()}"; rc,out,err = run_tool(self.tool.get(), ["--xuset-fdc", arg, self._current_device()]); self._log(out or err)

if __name__ == "__main__":
    App().mainloop()

