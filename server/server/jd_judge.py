#encoding=utf-8

import time
import os
import subprocess
import threading
import re
import uuid
import urllib.request, urllib.parse
import json
import base64
import markdown2

from . import jd_htmldocs as htmldocs
from . import jd_database as db
from . import jd_utils as utils

N_MAX_RUNNINGS = 20
PIGEON_URL = utils.read_file("pigeon-url.txt").split("\n")[0]

pendings = {}
runnings = {}
runnings_lock = threading.Lock()


def do_post(url, data_dict):
	while True:
		try:
			data = urllib.parse.urlencode(data_dict).encode()
			req = urllib.request.Request(url, data=data)
			res = urllib.request.urlopen(req, timeout=3)
			return res.read().decode('utf-8')
		except:
			time.sleep(1)

def do_get(url):
	while True:
		try:
			req = urllib.request.Request(url)
			res = urllib.request.urlopen(req, timeout=3)
			return res.read().decode('utf-8')
		except:
			time.sleep(1)

def try_get(url):
	try:
		req = urllib.request.Request(url)
		res = urllib.request.urlopen(req, timeout=3)
		return res.read().decode('utf-8')
	except:
		time.sleep(1)
		return None



def do_send_problem_file(md5):
	res = do_post(PIGEON_URL + "api/query_file", {"md5": md5})
	try:
		res = json.loads(res)
	except:
		# The pigeon might has gone
		return
	if res["status"] == "success":
		return
	content = base64.b64encode(utils.read_file_b(db.path_problem_zips + md5))
	do_post(PIGEON_URL + "api/send_file", {"md5": md5, "content": content})

def do_send_contestant_files(code_file, language):
	utils.write_file(db.path_temp + "language.txt", language)
	contestant_filename = "contestant.cpp"
	if language == "C":
		contestant_filename = "contestant.c"
	utils.system("cp", [code_file, db.path_temp + contestant_filename])
	zip_filename = db.path_temp + "to_submit.zip"
	utils.system("rm", ["-rf", zip_filename])
	utils.system("zip", ["-j", zip_filename, db.path_temp + "language.txt", db.path_temp + contestant_filename])
	content_b = utils.read_file_b(zip_filename)
	md5 = utils.md5sum_b(content_b)
	content = base64.b64encode(content_b)
	do_post(PIGEON_URL + "api/send_file", {"md5": md5, "content": content})
	return md5

def do_send_contestant_code(code, language):
	fname = db.path_temp + "temp_code.txt"
	utils.write_file(fname, code)
	return do_send_contestant_files(fname, language)

def do_submit_to_pigeon(sid):
	print("Submitting sid = %s" % sid)
	global pendings
	global runnings
	global runnings_lock
	try:
		priority = pendings[sid]
		del pendings[sid]
	except:
		priority = runnings[sid]["priority"]
	task_id = uuid.uuid1().hex
	sub = db.do_get_submission(sid)
	prob = db.do_get_problem_info(sub["pid"])
	# TODO: add this to database
	prob_md5 = "judgeduck-problems/" + sub["pid"]
	task = {
		"task_id": task_id,
		"priority": priority,
		"sid": sid,
		"problem_md5": prob_md5,
		"sub": sub,
		"language": sub["language"],
	}
	#do_send_problem_file(prob["md5"])
	task["contestant_md5"] = do_send_contestant_code(sub["code"], sub["language"])
	#task["contestant_md5"] = do_send_contestant_files(db.path_code + "%s.txt" % sid, sub["language"])
	res = do_post(PIGEON_URL + "api/submit_task", {
		"taskid": task["task_id"],
		"problem_md5": task["problem_md5"],
		"contestant_md5": task["contestant_md5"],
	})
	runnings_lock.acquire()
	runnings[sid] = task
	runnings_lock.release()

def judge_server_running_thread_func():
	global runnings
	global runnings_lock
	while True:
		time.sleep(1)
		runnings_lock.acquire()
		taskids = []
		tasks = []
		for sid in runnings:
			task = runnings[sid]
			taskids.append(task["task_id"])
			tasks.append(task)
		runnings_lock.release()
		if len(taskids) == 0:
			continue
		taskids_s = "|".join(taskids)
		res = do_post(PIGEON_URL + "api/get_task_results", {"taskids": taskids_s})
		try:
			res = json.loads(res)
		except:
			# gone
			continue
		if len(res) != len(taskids):
			continue
		print(json.dumps(res, indent=4, sort_keys=True))
		for i in range(len(res)):
			result = res[i]
			if result["status"] != "success":
				do_submit_to_pigeon(tasks[i]["sid"])
				continue
			result = result["result"]
			sid = tasks[i]["sid"]
			has_completed = result["has_completed"] == "true"
			db.do_update_sub_using_json(sid, result, has_completed)
			if has_completed:
				utils.system("rm", ["-rf", db.path_pending + "%s.txt" % sid])
				utils.system("rm", ["-rf", db.path_pending_rejudge + "%s.txt" % sid])
				runnings_lock.acquire()
				del runnings[sid]
				runnings_lock.release()

def judge_server_thread_func():
	global pendings
	global runnings
	global runnings_lock
	while True:
		time.sleep(0.2)
		runnings_lock.acquire()
		if len(runnings) >= N_MAX_RUNNINGS:
			runnings_lock.release()
			continue
		runnings_lock.release()
		files = utils.list_dir(db.path_pending)
		for filename in files:
			if filename[-4:] != ".txt": continue
			sid = utils.parse_int(filename[:-4], -1)
			if sid == -1: continue
			if runnings.get(sid, None) != None: continue
			if pendings.get(sid, None) != None: continue
			pendings[sid] = 50
		files = utils.list_dir(db.path_pending_rejudge)
		for filename in files:
			if filename[-4:] != ".txt": continue
			sid = utils.parse_int(filename[:-4], -1)
			if sid == -1: continue
			if runnings.get(sid, None) != None: continue
			if pendings.get(sid, None) != None: continue
			pendings[sid] = 30
		if len(pendings) == 0:
			continue
		max_sid = -1
		for sid in pendings:
			if (max_sid == -1) or (pendings[sid] > pendings[max_sid]):
				max_sid = sid
			if (max_sid != -1) and (pendings[sid] == pendings[max_sid]) and (sid < max_sid):
				max_sid = sid
		if max_sid == -1:
			continue
		do_submit_to_pigeon(max_sid)

server_status_str = "Loading ..."

def judge_monitor_thread_func():
	global server_status_str
	global runnings
	global pendings
	while True:
		res = try_get(PIGEON_URL)
		pigeon_avail = False
		if res != None:
			pigeon_avail = True
		server_avail = pigeon_avail
		tmp = ["## 服务状态", "", "---", ""]
		tmp.append("* 评测服务可用性: %s" % server_avail)
		tmp.append("* Judge Pigeon 可用性: %s" % pigeon_avail)
		tmp.append("* Running 数量: %s" % len(runnings))
		tmp.append("* Pending 数量: %s" % len(pendings))
		tmp.append("* Pending 文件个数: %s" % len(utils.list_dir(db.path_pending)))
		tmp.append("* Pending Rejudge 文件个数: %s" % len(utils.list_dir(db.path_pending_rejudge)))
		if not server_avail:
			tmp.append("* <font color='red'>**Warning: 评测服务不可用！**</font>")
		server_status_str = markdown2.markdown("\n".join(tmp))
		
		time.sleep(3)



judge_lock = threading.Lock()


class myThread(threading.Thread):
	def __init__(self, name, func):
		threading.Thread.__init__(self)
		self.name = name
		self.func = func
	def run(self):
		self.func()

def start_thread_func():
	print("Starting judge server threads")
	judge_server_thread = myThread("judgesrv", judge_server_thread_func)
	judge_server_thread.start()
	judge_server_running_thread = myThread("judgesrv-running", judge_server_running_thread_func)
	judge_server_running_thread.start()
	judge_monitor_thread = myThread("judge-monitor-thread", judge_monitor_thread_func)
	judge_monitor_thread.start()

def start():
	start_thread = myThread("start-judgesrv", start_thread_func)
	start_thread.start()
