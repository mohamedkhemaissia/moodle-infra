"""Deux profils fixes PHP-FPM, avec controles et retour au contenu precedent."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import time

from diagnostics.collector import get_json, ProbeError
from diagnostics.policy import Blocked, timestamp
from diagnostics.store import strict_json
from pilotage.operating_policy import PolicyStore, PolicyBlocked, PolicyStorageError
from .action import config_read, command, inspect, operations_clear, trusted_probe, NAMES
from .policy import decision_for_correction, metric_gate, PROFILE_ACTION
from .probes import login_probe
from .store import RemediationStore

TARGET = Path("/opt/moodle/config/php/zz-moodle.conf")
DESTINATION = "/usr/local/etc/php-fpm.d/zz-moodle.conf"
NORMAL, BURST = 5, 8
MIB = 1024 * 1024
PROFILE_OWNER_UID = 0

# PHP-FPM's private status listener remains usable when the application pool is busy.
# All request fields are fixed. Only bounded JSON status fields are returned.
STATUS_PHP = r'''
$s=@fsockopen("127.0.0.1",9001,$e,$m,2);if(!$s)exit(2);stream_set_timeout($s,3);
function rec($t,$b){return pack("CCnnCC",1,$t,1,strlen($b),0,0).$b;}
function take($s,$n){$v="";while(strlen($v)<$n){$x=fread($s,$n-strlen($v));if($x===false||$x===""||stream_get_meta_data($s)["timed_out"])exit(3);$v.=$x;}return $v;}
$p="";foreach(["REQUEST_METHOD"=>"GET","SCRIPT_NAME"=>"/fpm-status","SCRIPT_FILENAME"=>"/fpm-status","QUERY_STRING"=>"json","SERVER_PROTOCOL"=>"HTTP/1.1","CONTENT_LENGTH"=>"0"] as $k=>$v)$p.=chr(strlen($k)).chr(strlen($v)).$k.$v;
$q=rec(1,pack("nCxxxxx",1,0)).rec(4,$p).rec(4,"").rec(5,"");
while($q!==""){$n=fwrite($s,$q);if(!$n)exit(3);$q=substr($q,$n);}
$out="";for($i=0;$i<128;$i++){$h=unpack("Cv/Ct/nid/nlen/Cpad/Cr",take($s,8));if($h["v"]!==1||$h["id"]!==1||$h["len"]>32768)exit(4);$b=take($s,$h["len"]);if($h["pad"])take($s,$h["pad"]);if($h["t"]===6)$out.=$b;if(strlen($out)>32768)exit(4);if($h["t"]===3)break;}
fclose($s);$a=preg_split("/\r?\n\r?\n/",$out,2);if(count($a)!==2)exit(4);$d=json_decode($a[1],true,32,JSON_THROW_ON_ERROR);
$r=[];foreach(["pool","start time","listen queue","active processes","idle processes","total processes"] as $k){if(!array_key_exists($k,$d))exit(4);$r[str_replace(" ","_",$k)]=$d[$k];}echo json_encode($r,JSON_THROW_ON_ERROR);
'''


def sha(data):
    return hashlib.sha256(data).hexdigest()


def bounded(path, maximum=32768):
    parent = path.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != PROFILE_OWNER_UID or parent.st_mode & 0o022:
        raise Blocked("profile_directory_not_protected")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != PROFILE_OWNER_UID or info.st_mode & 0o022 or info.st_nlink != 1:
            raise Blocked("profile_file_not_protected")
        data = os.read(fd, maximum + 1)
        if len(data) > maximum:
            raise Blocked("profile_file_too_large")
        return data, (info.st_dev, info.st_ino)
    finally:
        os.close(fd)


def atomic_json(path, value, mode=0o600):
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False, sort_keys=True)
            stream.flush(); os.fsync(stream.fileno())
            if mode == 0o640: os.fchown(stream.fileno(), 0, 10001)
        os.replace(temporary, path)
        fd_dir = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(fd_dir)
        finally: os.close(fd_dir)
    finally:
        temporary.unlink(missing_ok=True)


class Profiles:
    def __init__(self, directory, *, target=TARGET):
        self.root = Path(directory) / "php-profiles"
        self.target = target

    def load(self):
        raw, _ = bounded(self.root / "catalog.json")
        value = strict_json(raw.decode())
        if (value["format"] != "php-fpm-profiles-v1" or value["target"] != str(self.target)
                or type(value["activation_revision"]) is not int or value["activation_revision"] < 1
                or set(value["profiles"]) != {"normal", "burst"}):
            raise Blocked("profile_catalog_invalid")
        for name, limit in (("normal", NORMAL), ("burst", BURST)):
            p = value["profiles"][name]
            if (type(p["max_children"]) is not int or p["max_children"] != limit
                    or not isinstance(p["content"], str) or len(p["content"].encode()) > 8192
                    or sha(p["content"].encode()) != p["sha256"]):
                raise Blocked("profile_catalog_invalid")
        normal = value["profiles"]["normal"]["content"]
        if (len(re.findall(r"(?m)^pm\.max_children\s*=", normal)) != 1
                or not re.search(r"(?m)^pm\.max_children\s*=\s*5\s*$", normal)
                or not re.search(r"(?m)^pm\s*=\s*dynamic\s*$", normal)
                or re.sub(r"(?m)^(pm\.max_children\s*=\s*)5(\s*)$", r"\g<1>8\2", normal)
                    != value["profiles"]["burst"]["content"]):
            raise Blocked("profile_catalog_invalid")
        return value

    def current(self, catalog):
        data, identity = bounded(self.target, maximum=8192)
        names = [name for name, profile in catalog["profiles"].items() if sha(data) == profile["sha256"]]
        if len(names) != 1:
            raise Blocked("profile_configuration_drift")
        return names[0], identity

    def publish(self, catalog, name, state, *, run_id=None, details=None):
        atomic_json(self.root / "status.json", {
            "format": "php-fpm-profile-status-v1", "recorded_at": time.time(), "state": state,
            "recorded_profile": name, "activation_revision": catalog["activation_revision"],
            "profiles": [{"id": k, "max_children": v["max_children"], "sha256": v["sha256"]}
                         for k, v in catalog["profiles"].items()],
            "run_id": run_id, "details": details or {},
        }, mode=0o640)

    def write(self, before_sha, identity, content):
        # Docker mounts this individual file. Keep the inode; replacing it by rename
        # would leave the running container attached to the old configuration.
        fd = os.open(self.target, os.O_RDWR | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if ((info.st_dev, info.st_ino) != identity or info.st_uid != PROFILE_OWNER_UID or info.st_mode & 0o022
                    or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
                raise Blocked("profile_file_identity_changed")
            existing = os.read(fd, 8193)
            if sha(existing) != before_sha: raise Blocked("profile_configuration_changed")
            os.lseek(fd, 0, os.SEEK_SET)
            view = memoryview(content)
            while view:
                written = os.write(fd, view)
                if written <= 0: raise OSError("Incomplete write")
                view = view[written:]
            os.ftruncate(fd, len(content)); os.fsync(fd)
        finally:
            os.close(fd)


class Runtime:
    def __init__(self, config): self.config = config

    def execute(self, argv, *, input=None, timeout=8):
        return subprocess.run(argv, input=input, text=True, capture_output=True, timeout=timeout,
                              env={"PATH":"/usr/bin:/bin", "LANG":"C.UTF-8"}, check=False)

    def validate(self, container, content=None):
        if content is None:
            args = ["/usr/bin/docker", "exec", container, "php-fpm", "-t"]
        else:
            args = ["/usr/bin/docker", "exec", "-i", container, "sh", "-ec",
                    'umask 077; p="$(mktemp /tmp/pfe-fpm-check.XXXXXX)"; trap \'rm -f "$p"\' EXIT; cat > "$p"; php-fpm -t -y "$p"']
        if self.execute(args, input=content).returncode:
            raise Blocked("php_fpm_configuration_invalid")

    def master(self, container):
        result = self.execute(["/usr/bin/docker", "exec", container, "php", "-r",
                               'echo trim(file_get_contents("/proc/1/comm"));'])
        if result.returncode or not re.fullmatch(r"php-fpm(?:[0-9.]+)?", result.stdout.strip()):
            raise Blocked("php_master_process_not_confirmed")

    def effective(self, container, limit):
        result = self.execute(["/usr/bin/docker", "exec", container, "php-fpm", "-tt"])
        section = None; fields = {}
        for line in (result.stdout+"\n"+result.stderr).splitlines():
            line = re.sub(r"^.*NOTICE:\s*", "", line).strip()
            match = re.fullmatch(r"\[([^\]]+)\]", line)
            if match: section = match[1]
            elif section == "www":
                match = re.fullmatch(r"(pm|pm\.max_children)\s*=\s*(\S+)", line)
                if match:
                    if match[1] in fields: raise Blocked("effective_pool_configuration_ambiguous")
                    fields[match[1]] = match[2]
        if result.returncode or fields != {"pm":"dynamic", "pm.max_children":str(limit)}:
            raise Blocked("effective_pool_limit_not_confirmed")

    def mount(self, container, target):
        output = self.execute(["/usr/bin/docker", "inspect", "--format", "{{json .Mounts}}", container])
        if output.returncode: raise Blocked("php_mount_unavailable")
        rows = strict_json(output.stdout)
        selected = [m for m in rows if m.get("Destination") == DESTINATION]
        if (len(selected) != 1 or selected[0].get("Source") != str(target)
                or selected[0].get("Type") != "bind" or selected[0].get("RW") is not False):
            raise Blocked("php_profile_mount_not_confirmed")
        output = self.execute(["/usr/bin/docker", "exec", container, "sha256sum", DESTINATION])
        if output.returncode or not re.match(r"^[0-9a-f]{64}\s", output.stdout): raise Blocked("mounted_profile_unavailable")
        return output.stdout.split()[0]

    def pool(self, container):
        output = self.execute(["/usr/bin/docker", "exec", container, "php", "-r", STATUS_PHP], timeout=6)
        if output.returncode: raise Blocked("private_pool_status_unavailable")
        d = strict_json(output.stdout)
        keys = ("start_time", "listen_queue", "active_processes", "idle_processes", "total_processes")
        if (d.get("pool") != "www" or any(type(d.get(k)) is not int or d[k] < 0 for k in keys)
                or d["total_processes"] != d["active_processes"] + d["idle_processes"]):
            raise Blocked("private_pool_status_invalid")
        return d

    def budget(self, container):
        result = self.execute(["/usr/bin/docker", "exec", container, "php", "-r", 'echo ini_get("memory_limit");'])
        match = re.fullmatch(r"([1-9][0-9]{0,6})([KMG]?)", result.stdout.strip())
        if result.returncode or not match: raise Blocked("php_memory_limit_unbounded")
        per_child = int(match[1]) * {"":1,"K":1024,"M":MIB,"G":1024*MIB}[match[2]] + 32*MIB
        raw = Path("/proc/meminfo").read_text()
        available = int(re.search(r"(?m)^MemAvailable:\s+([0-9]+) kB$", raw)[1]) * 1024
        required = (BURST - NORMAL) * per_child + 512*MIB
        limit = self.execute(["/usr/bin/docker", "inspect", "--format", "{{.HostConfig.Memory}}", container])
        if limit.returncode or not limit.stdout.strip().isdigit(): raise Blocked("container_memory_limit_unavailable")
        cap = int(limit.stdout.strip())
        if available < required or (cap and cap < BURST * per_child + 128*MIB):
            raise Blocked("insufficient_memory_budget_for_profile")
        return {"available_bytes":available, "additional_budget_bytes":required,
                "estimated_per_child_bytes":per_child, "container_limit_bytes":cap}

    def reload(self, container):
        if command(["/usr/bin/docker", "kill", "--signal", "USR2", container], timeout=8).returncode:
            raise Blocked("php_fpm_reload_failed")

    def inspect(self): return {name: inspect(name) for name in NAMES}
    def probe(self):
        return login_probe(self.config["hostname"], self.config["https_port"], self.config["ca_file"],
                           self.config.get("probe_scheme", "https"))
    def clear(self): operations_clear(self.config)
    def read(self, path): return get_json(self.config["api_url"] + path, timeout=8)


def running(services, previous=None, *, allow_php_offline_maintenance=False):
    if set(services) != set(NAMES): raise Blocked("profile_dependencies_incomplete")
    for name, item in services.items():
        s = item["state"]
        if (s["Status"] != "running" or s["Running"] is not True or item["health"] not in (None,"healthy")
                or any(s.get(k) is not False for k in ("Restarting","Paused","OOMKilled","Dead"))
                or (name == "php" and item.get("offline_maintenance") is not False
                    and not allow_php_offline_maintenance)
                or (previous and item["id"] != previous[name]["id"])):
            raise Blocked("profile_dependency_not_ready:"+name)
    if services["postgres"]["health"] != "healthy": raise Blocked("database_health_not_confirmed")


def applications_gate(d, now):
    if d.get("source") != "prometheus" or not 0 <= now-timestamp(d["evaluated_at"]) <= 15:
        raise Blocked("application_observation_stale")
    for name in ("php_fpm","postgresql"):
        s = d["services"][name]; age = s["collection"]["scrape_age_seconds"]
        if (s["service_reachable"] is not True or s["collection"]["status"] != "up"
                or type(age) not in (int,float) or not math.isfinite(age) or not 0 <= age <= 30):
            raise Blocked("application_collection_unavailable")
    php = d["services"]["php_fpm"]["metrics"]
    for k in ("active_processes","idle_processes","total_processes","listen_queue"):
        if type(php[k]) not in (int,float) or not math.isfinite(php[k]) or php[k] < 0 or php[k] != int(php[k]):
            raise Blocked("pool_metrics_incomplete")
    if php["total_processes"] != php["active_processes"] + php["idle_processes"]:
        raise Blocked("pool_metrics_inconsistent")
    pg = d["services"]["postgresql"]["metrics"]
    count, limit = pg["server_connections"], pg["max_connections"]
    if (type(count) not in (int,float) or type(limit) not in (int,float) or not math.isfinite(count+limit)
            or not 0 <= count < limit or limit <= 0 or count/limit >= .7):
        raise Blocked("postgres_capacity_not_ready")
    return php, timestamp(d["evaluated_at"]) - d["services"]["php_fpm"]["collection"]["scrape_age_seconds"]


def resource_gate(d, now, *, after=False):
    metric_gate(d, now, available=True)
    m = d["metrics"]
    if (m["cpu_usage_percent"] >= (80 if after else 70)
            or m["memory_usage_percent"] >= (80 if after else 70) or m["disk_available_percent"] <= 15):
        raise Blocked("resources_not_ready_for_profile")


def prepare(config, *, runtime=None, profiles=None, policy_store=None):
    if not config.get("policy_required") or not config.get("php_profiles_enabled"):
        raise Blocked("profiles_require_policies_and_activation")
    runtime = runtime or Runtime(config); p = profiles or Profiles(config["data_dir"])
    runtime.clear(); services = runtime.inspect()
    # La preparation valide des fichiers temporaires et ne recharge jamais PHP.
    # Le mode maintenance reste bloquant pour toute application reelle de profil.
    running(services, allow_php_offline_maintenance=True)
    container = services["php"]["id"]
    runtime.master(container)
    p.root.mkdir(mode=0o750, exist_ok=True)
    info = p.root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != PROFILE_OWNER_UID or info.st_mode & 0o022:
        raise Blocked("profile_directory_not_protected")
    os.chown(p.root, 0, 10001, follow_symlinks=False)
    if (p.root / "catalog.json").exists():
        catalog = p.load(); name, _ = p.current(catalog)
        if runtime.mount(container, p.target) != catalog["profiles"][name]["sha256"]:
            raise Blocked("mounted_profile_changed")
        runtime.effective(container, catalog["profiles"][name]["max_children"])
        return {"status":"prepared", "changed":False, "recorded_profile":name}
    ps = policy_store or PolicyStore()
    policy = ps.view()["policy"]
    if policy["mode"] != "observation": raise Blocked("set_observation_before_installing_profiles")
    raw, _ = bounded(p.target, maximum=8192)
    text = raw.decode()
    if (len(re.findall(r"(?m)^pm\.max_children\s*=", text)) != 1
            or not re.search(r"(?m)^pm\.max_children\s*=\s*5\s*$", text)
            or not re.search(r"(?m)^pm\s*=\s*dynamic\s*$", text)):
        raise Blocked("expected_normal_profile_not_found")
    burst = re.sub(r"(?m)^(pm\.max_children\s*=\s*)5(\s*)$", r"\g<1>8\2", text)
    catalog = {"format":"php-fpm-profiles-v1", "target":str(p.target), "activation_revision":policy["revision"],
               "profiles":{name:{"max_children":limit,"sha256":sha(content.encode()),"content":content}
                           for name,limit,content in (("normal",NORMAL,text),("burst",BURST,burst))}}
    if runtime.mount(container, p.target) != sha(raw): raise Blocked("mounted_profile_changed")
    for profile in catalog["profiles"].values(): runtime.validate(container, profile["content"])
    runtime.validate(container)
    runtime.effective(container, NORMAL)
    pool = runtime.pool(container)
    if pool["total_processes"] > NORMAL: raise Blocked("normal_pool_runtime_not_confirmed")
    if ps.view()["policy"] != policy:
        raise Blocked("policy_changed_during_profile_preparation")
    if runtime.mount(container, p.target) != sha(raw): raise Blocked("mounted_profile_changed")
    atomic_json(p.root / "catalog.json", catalog)
    p.publish(catalog, "normal", "prepared")
    return {"status":"prepared", "changed":True, "recorded_profile":"normal", "activation_revision":policy["revision"]}


def apply(config, incident_id, identifier, expected, *, runtime=None, profiles=None, policy_store=None,
          store=None, pause=time.sleep, window=60):
    rt = runtime or Runtime(config); p = profiles or Profiles(config["data_dir"])
    store = store or RemediationStore(config["data_dir"])
    report = {"status":"blocked", "action":PROFILE_ACTION, "mode":expected.get("execution_mode","manual_guarded"),
              "action_dispatched":False, "recovered":False, "rollback":{"attempted":False}, "checks":{}}
    changed = False; services = None; catalog = None; identity = None
    try:
        if not config.get("policy_required") or not config.get("php_profiles_enabled"):
            raise Blocked("php_profiles_not_enabled")
        catalog = p.load(); name, identity = p.current(catalog)
        if name != "normal": raise Blocked("profile_already_at_limit")
        if (p.root / "pending.json").exists(): raise Blocked("previous_profile_change_requires_review")
        def fresh():
            doc = rt.read("/api/v1/decisions/"+str(incident_id))
            decision = decision_for_correction(doc,incident_id,time.time(),expected=expected)
            if decision["correction_action"] != PROFILE_ACTION: raise Blocked("pool_incident_required")
            metrics = rt.read("/api/v1/metrics/summary"); resource_gate(metrics,time.time())
            apps = rt.read("/api/v1/metrics/applications"); php,_ = applications_gate(apps,time.time())
            if php["listen_queue"] <= 0 or php["idle_processes"] != 0 or php["active_processes"] < NORMAL:
                raise Blocked("pool_no_longer_at_limit")
            return doc,decision,metrics,apps
        doc, decision, metrics, apps = fresh()
        report["decision"] = decision
        report["checks"].update(metrics_before=metrics,applications_before=apps)
        rt.clear(); services = rt.inspect(); running(services)
        container = services["php"]["id"]
        rt.master(container)
        if rt.mount(container,p.target) != catalog["profiles"]["normal"]["sha256"]: raise Blocked("mounted_profile_changed")
        before = rt.pool(container)
        if before["listen_queue"] <= 0 or before["idle_processes"] != 0 or before["active_processes"] != NORMAL:
            raise Blocked("private_pool_not_at_normal_limit")
        report["checks"]["pool_before"] = before
        report["checks"]["memory_budget"] = rt.budget(container)
        probe = rt.probe(); report["checks"]["https_before"] = probe
        if probe.get("success") is not True or not trusted_probe(config, probe):
            raise Blocked("moodle_access_not_confirmed")
        rt.validate(container,catalog["profiles"]["burst"]["content"])
        doc,decision,_,_ = fresh(); rt.clear(); running(rt.inspect(),services)
        policy = expected.get("operating_policy")
        if not isinstance(policy,dict) or policy.get("revision",0) <= catalog["activation_revision"]:
            raise Blocked("new_policy_confirmation_required_for_profiles")
        ps = policy_store or PolicyStore()
        auth = ps.dispatch(doc,policy,run_id=identifier,automatic=expected.get("execution_mode") == "automatic_guarded",
            reserve=lambda:store.reserve(identifier,incident_id,time.time(),container,action_id=PROFILE_ACTION))
        report["operating_authorization"] = auth
        decision.update(operating_policy=auth["policy"],execution_mode=report["mode"])
        report["decision"] = decision
        if time.time() >= timestamp(decision["valid_until"]): raise Blocked("decision_expired_before_profile_write")
        rt.clear(); running(rt.inspect(),services); rt.budget(container)
        current, current_identity = p.current(catalog)
        if current != "normal" or current_identity != identity:
            raise Blocked("profile_configuration_changed")
        pending = {"run_id":identifier,"container_id":container,"incident_id":incident_id,
                   "previous":"normal","target":"burst", "created_at":time.time()}
        atomic_json(p.root/"pending.json",pending)
        p.publish(catalog,"normal","changing",run_id=identifier)
        report["action_dispatched"] = True; changed = True
        p.write(catalog["profiles"]["normal"]["sha256"],identity,catalog["profiles"]["burst"]["content"].encode())
        if rt.mount(container,p.target) != catalog["profiles"]["burst"]["sha256"]: raise Blocked("mounted_profile_changed")
        rt.validate(container); rt.effective(container, BURST); dispatched = time.time(); rt.reload(container)
        report["profile_change"] = {"from":"normal","to":"burst","max_children_before":NORMAL,"max_children_after":BURST}
        deadline = time.monotonic()+window; consecutive = 0; previous_scrape = None; samples = []
        for _ in range(31):
            if time.monotonic() >= deadline: break
            running(rt.inspect(),services)
            metric = rt.read("/api/v1/metrics/summary"); resource_gate(metric,time.time(),after=True)
            apps_after = rt.read("/api/v1/metrics/applications"); php,scrape = applications_gate(apps_after,time.time())
            direct = rt.pool(container); probe = rt.probe()
            good = (scrape > dispatched and direct["start_time"] > before["start_time"]
                    and direct["listen_queue"] == 0 and direct["idle_processes"] >= 1
                    and direct["total_processes"] <= BURST and php["listen_queue"] == 0
                    and php["idle_processes"] >= 1 and php["total_processes"] <= BURST
                    and probe.get("success") is True and trusted_probe(config, probe))
            if not good: consecutive = 0
            elif previous_scrape is None or scrape > previous_scrape:
                consecutive += 1; previous_scrape = scrape
            samples.append({"at":time.time(),"pool":direct,"php_scrape_at":scrape,"https":probe,"confirmed":good})
            report["checks"]["profile_samples"] = samples
            if consecutive >= 3:
                name,current_identity = p.current(catalog)
                if name != "burst" or current_identity != identity: raise Blocked("profile_configuration_changed")
                rt.effective(container, BURST)
                p.publish(catalog,"burst","verified",run_id=identifier)
                (p.root/"pending.json").unlink()
                report["checks"].update(metrics_after=metric,applications_after=apps_after)
                report.update(status="completed",recovered=True,reason="profile_reload_queue_and_https_confirmed")
                changed = False
                return report
            pause(2)
        raise Blocked("profile_benefit_not_confirmed")
    except (Blocked,PolicyBlocked,PolicyStorageError,ProbeError,ValueError,KeyError,TypeError,OSError,subprocess.TimeoutExpired,KeyboardInterrupt) as exc:
        report.update(status="failed" if changed else "blocked",reason=str(exc)[:180] if isinstance(exc,(Blocked,PolicyBlocked)) else "profile_check_unavailable",
                      error_type=type(exc).__name__)
    finally:
        if changed:
            report["rollback"]["attempted"] = True
            try:
                container = services["php"]["id"]
                current_services = rt.inspect()
                if current_services["php"]["id"] != container: raise Blocked("php_identity_changed_before_rollback")
                name,current_identity = p.current(catalog)
                if current_identity != identity: raise Blocked("profile_file_identity_changed")
                try: rollback_start = rt.pool(container)["start_time"]
                except (Blocked,OSError,ValueError,subprocess.TimeoutExpired): rollback_start = int(time.time())
                if name == "burst":
                    p.write(catalog["profiles"]["burst"]["sha256"],identity,catalog["profiles"]["normal"]["content"].encode())
                report["rollback"]["configuration_restored"] = True
                if rt.mount(container, p.target) != catalog["profiles"]["normal"]["sha256"]:
                    raise Blocked("previous_profile_mount_not_confirmed")
                rt.validate(container); rt.effective(container, NORMAL); rt.master(container)
                # FPM exposes start time in whole seconds. A same-second reload
                # cannot prove that the previous profile became active again.
                if time.time() < rollback_start+1.1: pause(rollback_start+1.1-time.time())
                rt.reload(container)
                deadline = time.monotonic()+45; count = 0
                for _ in range(23):
                    if time.monotonic() >= deadline: break
                    running(rt.inspect(),services); probe = rt.probe(); pool = rt.pool(container)
                    good = (pool["start_time"] > rollback_start and pool["total_processes"] <= NORMAL
                            and probe.get("success") is True and trusted_probe(config, probe))
                    count = count+1 if good else 0
                    if count >= 3: break
                    pause(2)
                if count < 3: raise Blocked("previous_profile_https_not_confirmed")
                p.publish(catalog,"normal","rolled_back",run_id=identifier)
                (p.root/"pending.json").unlink(missing_ok=True)
                report["rollback"].update(confirmed=True,profile="normal",pool=pool,https=probe)
            except Exception as exc:
                report["rollback"].update(confirmed=False,reason="operator_review_required",error_type=type(exc).__name__)
                try: p.publish(catalog,None,"review_required",run_id=identifier)
                except Exception: pass
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config",type=Path,required=True)
    parser.add_argument("--prepare",action="store_true")
    parser.add_argument("--status",action="store_true")
    parser.add_argument("--expected",type=Path)
    parser.add_argument("--incident-id",type=int)
    parser.add_argument("--run-id")
    args = parser.parse_args(); os.umask(0o027)
    signal.signal(signal.SIGTERM,lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        config = config_read(args.config)
        if args.prepare: result = prepare(config)
        elif args.status:
            p=Profiles(config["data_dir"]); catalog=p.load(); name,_=p.current(catalog)
            result={"status":"available","recorded_profile":name,"profiles":[{"id":k,"max_children":v["max_children"]} for k,v in catalog["profiles"].items()],
                    "activation_revision":catalog["activation_revision"],"review_required":(p.root/"pending.json").exists()}
        else:
            if args.expected is None or args.incident_id is None or not re.fullmatch(r"[0-9a-f]{32}",args.run_id or ""):
                parser.error("Une execution exige une decision, un incident et une reference de journal.")
            result=apply(config,args.incident_id,args.run_id,strict_json(args.expected.read_text()))
    except (Blocked,OSError,ValueError,KeyError,PolicyStorageError) as exc:
        result={"status":"blocked","reason":str(exc)[:180] if isinstance(exc,Blocked) else "profile_configuration_unavailable"}
    print(json.dumps(result,ensure_ascii=False,allow_nan=False))
    return 0 if result["status"] in ("prepared","available","completed") else 2


if __name__ == "__main__":
    raise SystemExit(main())
