"""Append-only shot data, independent derived tables, and bounded exports."""
from dataclasses import asdict
from datetime import datetime, timezone
import json
import hashlib
import gzip
import os
from pathlib import Path
import sqlite3
import time

import pandas as pd
from comparator_characterization.storage import atomic_write_json, atomic_write_table


def utc():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def read_frame(db, scan_id):
    frame = pd.read_sql_query("""SELECT p.*, a.scan_id, a.kind, a.code, a.repeat,
        a.direction FROM samples p JOIN shots a ON a.id=p.shot_id
        WHERE a.scan_id=? ORDER BY a.id,p.column,p.row""",db,params=(scan_id,))
    metadata={sid:json.loads(result) for sid,result in
              db.execute("SELECT id,result_json FROM shots WHERE scan_id=?",(scan_id,))}
    denominators={sid:r.get("injections_for_analysis") for sid,r in metadata.items()}
    frame["nominal_n"] = frame.shot_id.map(denominators)
    frame["acquisition_valid"] = frame.shot_id.map({sid:r.get("details",{}).get("omr_valid",True) for sid,r in metadata.items()})
    return frame


class RunStore:
    def __init__(self, root, document, resume=False):
        self.root = Path(root)
        if resume:
            self.document=json.loads((self.root/"run.json").read_text(encoding="utf-8"))
            for key in ("settings","code_sha256","threshold_luts","reference_luts","reference_plan","mask_sha256"):
                old,new=self.document.get(key),document.get(key)
                if key in ("threshold_luts","reference_luts"):
                    old={k:{a:b for a,b in v.items() if a!="source_path"} for k,v in old.items()}
                    new={k:{a:b for a,b in v.items() if a!="source_path"} for k,v in new.items()}
                if json.dumps(old,sort_keys=True)!=json.dumps(new,sort_keys=True):
                    raise ValueError(f"Resume contract changed: {key}. Use --new or analyze the old run offline.")
            if self.document.get("last_error"):
                self.document.setdefault("previous_errors",[]).append(self.document.pop("last_error"))
            self.document["status"]="running"
        else:
            self.root.mkdir(parents=True, exist_ok=False)
            self.document = {**document, "started_utc": utc(), "status":"running", "scans":[]}
        self.save()
        self.db = sqlite3.connect(self.root/"raw.sqlite")
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS scans (id TEXT PRIMARY KEY, context_json TEXT, status TEXT);
            CREATE TABLE IF NOT EXISTS shots (id INTEGER PRIMARY KEY, scan_id TEXT, kind TEXT,
                code INTEGER, repeat INTEGER, direction TEXT, started_utc TEXT,
                elapsed_s REAL, request_json TEXT, result_json TEXT);
            CREATE TABLE IF NOT EXISTS samples (shot_id INTEGER, column INTEGER, row INTEGER,
                count REAL, valid INTEGER, saturated INTEGER, trim INTEGER,
                raw_hex TEXT, low_count REAL, mid_count REAL, high_count REAL,
                error TEXT, PRIMARY KEY(shot_id,column,row));
            CREATE INDEX IF NOT EXISTS scan_shots ON shots(scan_id);
        """)
        self.scan_count = len(self.document["scans"])
        self.event("session_start", resumed=resume)

    def save(self):
        atomic_write_json(self.root/"run.json", self.document)

    def event(self, kind, **data):
        with (self.root/"events.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"utc":utc(), "kind":kind, **data}, ensure_ascii=False, default=str)+"\n")

    def begin_scan(self, context):
        scan_key=hashlib.sha256(json.dumps(context,sort_keys=True).encode()).hexdigest()
        previous=next((x for x in self.document["scans"] if x.get("scan_key")==scan_key),None)
        if previous:
            return previous["scan_id"]
        self.scan_count += 1
        scan_id = f"s{self.scan_count:04d}"
        context = {**context, "scan_id":scan_id, "scan_key":scan_key, "status":"running"}
        with self.db:
            self.db.execute("INSERT INTO scans VALUES (?,?,?)", (scan_id,json.dumps(context),"running"))
        self.document["scans"].append(context)
        self.save()
        return scan_id

    def finish_scan(self, scan_id):
        with self.db:
            self.db.execute("UPDATE scans SET status='complete' WHERE id=?", (scan_id,))
        next(x for x in self.document["scans"] if x["scan_id"]==scan_id)["status"]="complete"
        self.save()

    def shot(self, scan_id, kind, code, repeat, direction, request, result, samples, trims, elapsed,started_utc=None):
        def number(value):
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
        with self.db:
            cur = self.db.execute("INSERT INTO shots VALUES (NULL,?,?,?,?,?,?,?,?,?)",
                (scan_id,kind,int(code),int(repeat),direction,started_utc or utc(),elapsed,
                 json.dumps(asdict(request),default=str),json.dumps(asdict(result),default=str)))
            sid = cur.lastrowid
            self.db.executemany("INSERT INTO samples VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", [
                (sid,int(s["column"]),int(s["row"]),number(s.get("selected_count")),
                 int(bool(s.get("measurement_valid")) and bool(s.get("counter_decode_valid"))),
                 int(bool(s.get("counter_saturated"))),int(trims[(s["column"],s["row"])]),
                 s.get("raw_counter_word_hex",""),number(s.get("counter_low_count")),
                 number(s.get("counter_mid_count")),number(s.get("counter_high_count")),
                 s.get("measurement_error","")) for s in samples])
        return sid

    def raw_frame(self, scan_id):
        return read_frame(self.db,scan_id)

    def table(self, name, frame):
        path=self.root/"analysis"/name
        if path.suffix==".gz":
            path.parent.mkdir(parents=True,exist_ok=True)
            temporary=path.with_name("."+path.name+".tmp")
            temporary.write_bytes(gzip.compress(frame.to_csv(index=False).encode("utf-8"),mtime=0))
            os.replace(temporary,path)
            return path
        return atomic_write_table(path, frame)

    def close(self):
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.db.close()


def load_scan(root, scan_id):
    uri = (Path(root)/"raw.sqlite").resolve().as_uri()+"?mode=ro"
    with sqlite3.connect(uri,uri=True) as db:
        return read_frame(db,scan_id)
