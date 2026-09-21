import csv
import io
import secrets
from datetime import datetime, timezone
from functools import wraps
from flask import Blueprint, abort, current_app, flash, make_response, redirect, render_template, request, send_from_directory, session, url_for
from .database import get_db, using_postgres

bp = Blueprint("experiment", __name__)


def now(): return datetime.now(timezone.utc)
def stamp(): return now().isoformat()


def session_row():
    sid = session.get("sid")
    if not sid: return None
    return get_db().execute("""SELECT s.*, p.participant_code, c.code condition_code, c.source_type, c.error_level, c.map_filename
        FROM sessions s JOIN participants p ON p.id=s.participant_id LEFT JOIN conditions c ON c.id=s.condition_id WHERE s.id=?""", (sid,)).fetchone()


def guard(*states):
    row = session_row()
    if not row: return None, redirect(url_for("experiment.login"))
    if states and row["status"] not in states: return None, redirect(next_url(row))
    return row, None


def next_url(row):
    if row["status"] == "baseline_visual": return url_for("experiment.baseline_visual", index=0)
    if row["status"] == "scenarios": return url_for("experiment.scenario", index=0)
    pages = {"baseline_profile":"experiment.baseline_profile", "baseline_literacy":"experiment.baseline_literacy", "baseline_visual":"experiment.baseline_visual", "intro":"experiment.introduction", "label":"experiment.source_label", "map_instruction":"experiment.map_instruction", "observe":"experiment.observe", "decision_revisit_choice":"experiment.decision_revisit_choice", "decision_revisit":"experiment.decision_revisit", "scenarios":"experiment.scenario", "revisit_reason":"experiment.revisit_reason", "questionnaires":"experiment.questionnaires", "open":"experiment.open_questions", "complete":"experiment.complete"}
    return url_for(pages.get(row["status"], "experiment.login"))


def new_code(db):
    while True:
        code = "P-" + secrets.token_hex(4).upper()
        if not db.execute("SELECT 1 FROM participants WHERE participant_code=?", (code,)).fetchone(): return code


@bp.route("/", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        age = request.form.get("age", type=int); gender = request.form.get("gender", ""); education = request.form.get("education_level", "")
        if not request.form.get("consent") or age is None or age < 16 or gender not in ("男", "女") or not education:
            flash("请完成所有信息并确认知情同意；参与者年龄须为 16 岁及以上。")
            return render_template("login.html")
        db = get_db(); code = new_code(db)
        participant_values = (code, age, gender, education, stamp(), stamp())
        if using_postgres():
            person = db.execute(
                "INSERT INTO participants(participant_code,age,gender,education_level,consent_at,created_at) VALUES(?,?,?,?,?,?) RETURNING id",
                participant_values,
            ).fetchone()
            participant_id = person["id"]
            trial = db.execute(
                "INSERT INTO sessions(participant_id,status) VALUES(?,?) RETURNING id",
                (participant_id, "baseline_profile"),
            ).fetchone()
            session_id = trial["id"]
        else:
            # Python 3.8 bundles an older SQLite that has no RETURNING clause.
            # Cursor.lastrowid works on both the local test database and
            # PythonAnywhere's SQLite database.
            person_cursor = db.execute(
                "INSERT INTO participants(participant_code,age,gender,education_level,consent_at,created_at) VALUES(?,?,?,?,?,?)",
                participant_values,
            )
            trial_cursor = db.execute(
                "INSERT INTO sessions(participant_id,status) VALUES(?,?)",
                (person_cursor.lastrowid, "baseline_profile"),
            )
            session_id = trial_cursor.lastrowid
        
        db.commit(); session.clear(); session["sid"] = session_id
        return redirect(url_for("experiment.introduction"))
    return render_template("login.html")


@bp.route("/baseline/profile", methods=["GET", "POST"])
def baseline_profile():
    row, bounce = guard("baseline_profile")
    if bounce: return bounce
    items = current_app.config["BASELINE_PROFILE_ITEMS"]
    if request.method == "POST":
        responses = []
        for code, text, choices in items:
            value = request.form.get(code, "")
            label = dict(choices).get(value)
            if not label:
                flash("请完成全部基线问题。")
                return render_template("baseline_profile.html", items=items)
            responses.append((row["id"], code, text, value, label, stamp()))
        db = get_db(); db.executemany("INSERT INTO baseline_profile_responses(session_id,item_code,item_text,response_value,response_label,answered_at) VALUES(?,?,?,?,?,?)", responses)
        db.execute("UPDATE sessions SET status='baseline_literacy' WHERE id=?", (row["id"],)); db.commit()
        return redirect(url_for("experiment.baseline_literacy"))
    return render_template("baseline_profile.html", items=items)


@bp.route("/baseline/map-literacy", methods=["GET", "POST"])
def baseline_literacy():
    row, bounce = guard("baseline_literacy")
    if bounce: return bounce
    items = current_app.config["MAP_LITERACY_ITEMS"]
    if request.method == "POST":
        values = []
        for code, text in items:
            score = request.form.get(code, type=int)
            if score not in range(1, 8):
                flash("请完成全部地图素养条目。")
                return render_template("baseline_literacy.html", items=items)
            values.append((row["id"], "map_literacy", code, text, score, stamp()))
        db = get_db(); db.executemany("INSERT INTO scale_responses(session_id,scale_code,item_code,item_text,score,answered_at) VALUES(?,?,?,?,?,?)", values)
        db.execute("UPDATE sessions SET status='baseline_visual' WHERE id=?", (row["id"],)); db.commit()
        return redirect(url_for("experiment.baseline_visual", index=0))
    return render_template("baseline_literacy.html", items=items)


@bp.route("/baseline/visual/<int:index>", methods=["GET", "POST"])
def baseline_visual(index):
    row, bounce = guard("baseline_visual")
    if bounce: return bounce
    tasks = current_app.config["BASELINE_VISUAL_TASKS"]
    if index < 0 or index >= len(tasks): abort(404)
    code, title, prompt, options, correct = tasks[index]
    if request.method == "POST":
        answer = request.form.get("answer"); started_ms = request.form.get("started_ms", type=int)
        if answer not in {value for value, _label in options} or not started_ms:
            flash("请选择答案后继续。")
            return render_template("baseline_visual.html", index=index, total=len(tasks), title=title, prompt=prompt, options=options, baseline_map_url=url_for("experiment.stimulus", filename="基线测试.png"))
        submitted = now(); duration = max(0, int(submitted.timestamp() * 1000 - started_ms))
        db = get_db(); db.execute("INSERT INTO baseline_visual_responses(session_id,task_code,selected_answer,correct_answer,started_at,submitted_at,response_time_ms) VALUES(?,?,?,?,?,?,?)", (row["id"], code, answer, correct, datetime.fromtimestamp(started_ms/1000, timezone.utc).isoformat(), submitted.isoformat(), duration))
        if index + 1 == len(tasks): db.execute("UPDATE sessions SET status='intro' WHERE id=?", (row["id"],))
        db.commit()
        return redirect(url_for("experiment.baseline_visual", index=index+1) if index+1 < len(tasks) else url_for("experiment.introduction"))
    return render_template("baseline_visual.html", index=index, total=len(tasks), title=title, prompt=prompt, options=options, baseline_map_url=url_for("experiment.stimulus", filename="基线测试.png"))


@bp.route("/introduction", methods=["GET", "POST"])
def introduction():
    row, bounce = guard("intro")
    if bounce: return bounce
    if request.method == "POST":
        if not request.form.get("understood"):
            flash("请确认已阅读任务说明。")
            return render_template("introduction.html")
        db = get_db()
        # Balance all assigned sessions, including incomplete ones, to prevent early exits
        # from causing one condition to be repeatedly reassigned.
        condition = db.execute("""
            WITH condition_counts AS (
                SELECT c.*, COUNT(s.id) AS assigned_count
                FROM conditions c
                LEFT JOIN sessions s ON s.condition_id = c.id
                GROUP BY c.id
            )
            SELECT * FROM condition_counts
            WHERE assigned_count = (SELECT MIN(assigned_count) FROM condition_counts)
            ORDER BY RANDOM()
            LIMIT 1
        """).fetchone()
        label = "人工智能系统生成" if condition["source_type"] == "ai" else "人类制作者绘制"
        db.execute("UPDATE sessions SET condition_id=?, source_label=?, intro_confirmed_at=?, assigned_at=?, status='label' WHERE id=?", (condition["id"], label, stamp(), stamp(), row["id"]))
        db.commit(); return redirect(url_for("experiment.source_label"))
    return render_template("introduction.html")


@bp.route("/source-label", methods=["GET", "POST"])
def source_label():
    row, bounce = guard("label")
    if bounce: return bounce
    if request.method == "POST":
        get_db().execute("UPDATE sessions SET status='observe' WHERE id=?", (row["id"],)); get_db().commit()
        return redirect(url_for("experiment.observe"))
    return render_template("source_label.html", label=row["source_label"])


@bp.route("/map-instruction", methods=["GET", "POST"])
def map_instruction():
    row, bounce = guard("map_instruction")
    if bounce: return bounce
    # Supports any unfinished session created during the previous flow version.
    get_db().execute("UPDATE sessions SET status='observe' WHERE id=?", (row["id"],)); get_db().commit()
    return redirect(url_for("experiment.observe"))


@bp.route("/observe", methods=["GET", "POST"])
def observe():
    row, bounce = guard("observe")
    if bounce: return bounce
    db = get_db()
    if not row["map_opened_at"]:
        db.execute("UPDATE sessions SET map_opened_at=? WHERE id=?", (stamp(), row["id"])); db.commit()
        row = session_row()
    observation_ms = current_app.config["OBSERVATION_SECONDS"] * 1000
    elapsed = max(0, int((now() - datetime.fromisoformat(row["map_opened_at"])).total_seconds() * 1000))
    if request.method == "POST":
        # The server, rather than only the browser timer, enforces the full
        # observation window.  This also makes page and image loading time part
        # of the fixed 40-second window.
        if elapsed < observation_ms:
            return render_template(
                "observe.html",
                map_url=url_for("experiment.stimulus", filename=row["map_filename"]),
                seconds=current_app.config["OBSERVATION_SECONDS"],
                remaining_ms=observation_ms - elapsed,
            )
        if not row["map_closed_at"]:
            db.execute("UPDATE sessions SET map_closed_at=?, map_view_duration_ms=?, status='scenarios' WHERE id=?", (stamp(), elapsed, row["id"])); db.commit()
        return redirect(url_for("experiment.scenario", index=0))
    return render_template(
        "observe.html",
        map_url=url_for("experiment.stimulus", filename=row["map_filename"]),
        seconds=current_app.config["OBSERVATION_SECONDS"],
        remaining_ms=max(0, observation_ms - elapsed),
    )


@bp.route("/decision-map-choice", methods=["GET", "POST"])
def decision_revisit_choice():
    row, bounce = guard("decision_revisit_choice")
    if bounce: return bounce
    if request.method == "POST":
        choice = request.form.get("review_choice")
        db = get_db()
        if choice == "direct":
            db.execute("UPDATE sessions SET status='scenarios' WHERE id=?", (row["id"],)); db.commit()
            return redirect(url_for("experiment.scenario", index=0))
        if choice == "revisit":
            existing = db.execute("SELECT 1 FROM revisit_events WHERE session_id=? AND scenario_code='pre_decision'", (row["id"],)).fetchone()
            if not existing:
                db.execute("INSERT INTO revisit_events(session_id,scenario_code,opened_at) VALUES(?,?,?)", (row["id"], "pre_decision", stamp()))
            db.execute("UPDATE sessions SET status='decision_revisit' WHERE id=?", (row["id"],)); db.commit()
            return redirect(url_for("experiment.decision_revisit"))
        flash("请选择“直接作答”或“再次查看地图”。")
    return render_template("predecision_choice.html")


@bp.route("/decision-map-revisit", methods=["GET", "POST"])
def decision_revisit():
    row, bounce = guard("decision_revisit")
    if bounce: return bounce
    db = get_db()
    event = db.execute("SELECT * FROM revisit_events WHERE session_id=? ORDER BY id DESC LIMIT 1", (row["id"],)).fetchone()
    if not event: return redirect(url_for("experiment.scenario", index=0))
    scenarios = current_app.config["SCENARIOS"]
    revisit_index = next((index for index, item in enumerate(scenarios) if item["code"] == event["scenario_code"]), 0)
    if request.method == "POST":
        elapsed = max(0, int((now() - datetime.fromisoformat(event["opened_at"])).total_seconds() * 1000))
        required_ms = current_app.config["PREDECISION_REVISIT_SECONDS"] * 900
        if elapsed < required_ms:
            flash("地图仍在展示中，请稍候。")
            return render_template("predecision_revisit.html", map_url=url_for("experiment.stimulus", filename=row["map_filename"]), seconds=current_app.config["PREDECISION_REVISIT_SECONDS"])
        db.execute("UPDATE revisit_events SET closed_at=?, duration_ms=? WHERE id=?", (stamp(), elapsed, event["id"]))
        db.execute("UPDATE sessions SET status='scenarios' WHERE id=?", (row["id"],)); db.commit()
        return redirect(url_for("experiment.scenario", index=revisit_index))
    return render_template("predecision_revisit.html", map_url=url_for("experiment.stimulus", filename=row["map_filename"]), seconds=current_app.config["PREDECISION_REVISIT_SECONDS"])


@bp.route("/scenario/<int:index>", methods=["GET", "POST"])
def scenario(index):
    row, bounce = guard("scenarios")
    if bounce: return bounce
    scenarios = current_app.config["SCENARIOS"]
    if index < 0 or index >= len(scenarios): abort(404)
    item = scenarios[index]; db = get_db()
    revisit_used = db.execute("SELECT 1 FROM revisit_events WHERE session_id=?", (row["id"],)).fetchone() is not None
    existing = db.execute("SELECT 1 FROM scenario_responses WHERE session_id=? AND scenario_code=?", (row["id"], item["code"])).fetchone()
    if existing:
        if index + 1 < len(scenarios): return redirect(url_for("experiment.scenario", index=index + 1))
        revisited = db.execute("SELECT 1 FROM revisit_events WHERE session_id=?", (row["id"],)).fetchone()
        return redirect(url_for("experiment.revisit_reason") if revisited else url_for("experiment.questionnaires"))
    timing = db.execute("SELECT started_at FROM scenario_timing_state WHERE session_id=? AND scenario_code=?", (row["id"], item["code"])).fetchone()
    if not timing:
        db.execute("INSERT INTO scenario_timing_state(session_id,scenario_code,started_at) VALUES(?,?,?)", (row["id"], item["code"], stamp()))
        db.commit()
        timing = db.execute("SELECT started_at FROM scenario_timing_state WHERE session_id=? AND scenario_code=?", (row["id"], item["code"])).fetchone()
    if request.method == "POST":
        choice = request.form.get("selected_exit"); confidence = request.form.get("confidence", type=int)
        if choice not in {x[0] for x in item["options"]} or confidence not in range(1, 8):
            flash("请选择出口，并完成决策信心评分。")
            return render_template("scenario.html", item=item, index=index, total=len(scenarios), revisit_used=revisit_used)
        submitted = now(); started_at = datetime.fromisoformat(timing["started_at"]); duration = max(0, int((submitted - started_at).total_seconds() * 1000))
        db.execute("INSERT INTO scenario_responses(session_id,scenario_code,selected_exit,confidence_score,started_at,submitted_at,decision_time_ms) VALUES(?,?,?,?,?,?,?)", (row["id"], item["code"], choice, confidence, timing["started_at"], submitted.isoformat(), duration))
        if index + 1 == len(scenarios):
            revisited = db.execute("SELECT 1 FROM revisit_events WHERE session_id=?", (row["id"],)).fetchone()
            db.execute("UPDATE sessions SET status=? WHERE id=?", ("revisit_reason" if revisited else "questionnaires", row["id"]))
        db.commit()
        if index + 1 < len(scenarios): return redirect(url_for("experiment.scenario", index=index + 1))
        return redirect(url_for("experiment.revisit_reason") if revisited else url_for("experiment.questionnaires"))
    return render_template("scenario.html", item=item, index=index, total=len(scenarios), revisit_used=revisit_used)


@bp.post("/scenario/<int:index>/revisit")
def start_revisit(index):
    row, bounce = guard("scenarios")
    if bounce: return bounce
    scenarios = current_app.config["SCENARIOS"]
    if index < 0 or index >= len(scenarios): abort(404)
    db = get_db()
    if db.execute("SELECT 1 FROM revisit_events WHERE session_id=?", (row["id"],)).fetchone():
        flash("本次实验的再次查看地图机会已使用。")
        return redirect(url_for("experiment.scenario", index=index))
    db.execute("INSERT INTO revisit_events(session_id,scenario_code,opened_at) VALUES(?,?,?)", (row["id"], scenarios[index]["code"], stamp()))
    db.execute("UPDATE sessions SET status='decision_revisit' WHERE id=?", (row["id"],)); db.commit()
    return redirect(url_for("experiment.decision_revisit"))


@bp.route("/revisit-reason", methods=["GET", "POST"])
def revisit_reason():
    row, bounce = guard("revisit_reason")
    if bounce: return bounce
    if request.method == "POST":
        reason = request.form.get("reason_code")
        other = request.form.get("other_reason", "").strip()
        if reason not in {"memory", "reliability", "both", "other"} or (reason == "other" and not other):
            flash("请选择主要原因；如选择“其他原因”，请填写具体内容。")
            return render_template("revisit_reason.html")
        db = get_db()
        db.execute("""INSERT INTO revisit_reason_responses(session_id,reason_code,other_reason,answered_at) VALUES(?,?,?,?)
            ON CONFLICT(session_id) DO UPDATE SET reason_code=excluded.reason_code, other_reason=excluded.other_reason, answered_at=excluded.answered_at""", (row["id"], reason, other if reason == "other" else None, stamp()))
        db.execute("UPDATE sessions SET status='questionnaires' WHERE id=?", (row["id"],)); db.commit()
        return redirect(url_for("experiment.questionnaires"))
    return render_template("revisit_reason.html")


@bp.route("/questionnaires", methods=["GET", "POST"])
def questionnaires():
    row, bounce = guard("questionnaires")
    if bounce: return bounce
    forms = current_app.config["QUESTIONNAIRES"]
    if request.method == "POST":
        values = []
        for scale, _title, items in forms:
            for code, text, *_ in items:
                score = request.form.get(code, type=int)
                if score not in range(1, 8):
                    flash("请完成全部量表条目。")
                    return render_template("questionnaires.html", forms=forms, map_url=url_for("experiment.stimulus", filename=row["map_filename"]))
                values.append((row["id"], scale, code, text, score, stamp()))
        source = request.form.get("perceived_source")
        if source not in ("ai", "human"):
            flash("请完成操纵检验问题。")
            return render_template("questionnaires.html", forms=forms, map_url=url_for("experiment.stimulus", filename=row["map_filename"]))
        db = get_db(); db.executemany("INSERT INTO scale_responses(session_id,scale_code,item_code,item_text,score,answered_at) VALUES(?,?,?,?,?,?)", values)
        db.execute("INSERT INTO manipulation_checks(session_id,perceived_source,perceived_error,answered_at) VALUES(?,?,?,?)", (row["id"], source, "recorded_in_ES_scale", stamp()))
        db.execute("UPDATE sessions SET status='open' WHERE id=?", (row["id"],)); db.commit()
        return redirect(url_for("experiment.open_questions"))
    return render_template("questionnaires.html", forms=forms, map_url=url_for("experiment.stimulus", filename=row["map_filename"]))


@bp.route("/open-questions", methods=["GET", "POST"])
def open_questions():
    row, bounce = guard("open")
    if bounce: return bounce
    if request.method == "POST":
        description = request.form.get("production_description", "").strip(); change = request.form.get("ai_trust_change", "").strip()
        if not description or not change:
            flash("请回答两个开放性问题。")
            return render_template("open_questions.html")
        db = get_db(); db.execute("INSERT INTO open_responses(session_id,production_description,ai_trust_change,answered_at) VALUES(?,?,?,?)", (row["id"], description, change, stamp()))
        db.execute("UPDATE sessions SET status='complete', completed_at=? WHERE id=?", (stamp(), row["id"])); db.commit()
        return redirect(url_for("experiment.complete"))
    return render_template("open_questions.html")


@bp.route("/complete")
def complete():
    row, bounce = guard("complete")
    if bounce: return bounce
    return render_template("complete.html", code=row["participant_code"])


@bp.route("/stimuli/<path:filename>")
def stimulus(filename):
    allowed = {x[3] for x in current_app.config["CONDITIONS"]} | {"基线测试.png"}
    if filename not in allowed: abort(404)
    return send_from_directory(current_app.config["STIMULUS_DIRECTORY"], filename)


def researcher_auth(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if request.args.get("key") != current_app.config["RESEARCHER_PASSWORD"]:
            abort(403)
        return fn(*args, **kwargs)
    return wrapped


@bp.route("/researcher")
@researcher_auth
def researcher():
    db = get_db()
    rows = db.execute("SELECT c.code, COUNT(s.id) total, SUM(CASE WHEN s.status='complete' THEN 1 ELSE 0 END) completed FROM conditions c LEFT JOIN sessions s ON s.condition_id=c.id GROUP BY c.id ORDER BY c.code").fetchall()
    return render_template("researcher.html", rows=rows)


@bp.route("/researcher/export.csv")
@researcher_auth
def export_csv():
    aggregate = "STRING_AGG" if using_postgres() else "GROUP_CONCAT"
    text_cast = "::text" if using_postgres() else ""
    db=get_db(); rows=db.execute(f"""SELECT p.participant_code,p.age,p.gender,p.education_level,c.code condition_code,c.source_type,c.error_level,c.map_filename,s.map_view_duration_ms,
      {aggregate}(sr.scenario_code || ':' || sr.selected_exit, ' | ') choices,{aggregate}(sr.decision_time_ms{text_cast}, ' | ') decision_times_ms,
      (SELECT COUNT(*) FROM revisit_events r WHERE r.session_id=s.id) revisit_count,(SELECT COALESCE(SUM(duration_ms),0) FROM revisit_events r WHERE r.session_id=s.id) revisit_total_ms,
      (SELECT {aggregate}(item_code || ':' || score{text_cast}, ' | ') FROM scale_responses q WHERE q.session_id=s.id) scale_scores,
      (SELECT {aggregate}(task_code || ':' || selected_answer || ':' || response_time_ms{text_cast}, ' | ') FROM baseline_visual_responses b WHERE b.session_id=s.id) baseline_visual_search,
      CASE rr.reason_code WHEN 'memory' THEN '没有完全记清地图中的位置或路径信息' WHEN 'reliability' THEN '希望进一步确认地图信息是否可靠' WHEN 'both' THEN '两方面都有' WHEN 'other' THEN '其他原因' END revisit_reason,rr.other_reason,mc.perceived_source,mc.perceived_error,o.production_description,o.ai_trust_change,s.completed_at
      FROM sessions s JOIN participants p ON p.id=s.participant_id LEFT JOIN conditions c ON c.id=s.condition_id LEFT JOIN scenario_responses sr ON sr.session_id=s.id LEFT JOIN revisit_reason_responses rr ON rr.session_id=s.id LEFT JOIN manipulation_checks mc ON mc.session_id=s.id LEFT JOIN open_responses o ON o.session_id=s.id GROUP BY s.id,p.id,c.id,rr.id,mc.id,o.id ORDER BY s.id""").fetchall()
    out=io.StringIO(); writer=csv.writer(out); writer.writerow(["participant_code","age","gender","education_level","condition","source","error_level","map_filename","map_view_duration_ms","choices","decision_times_ms","revisit_count","revisit_total_ms","scale_scores","baseline_visual_search","revisit_reason","revisit_other_reason","perceived_source","perceived_error","production_description","ai_trust_change","completed_at"]); writer.writerows([list(x) for x in rows])
    result=make_response("\ufeff"+out.getvalue()); result.headers["Content-Type"]="text/csv; charset=utf-8"; result.headers["Content-Disposition"]="attachment; filename=experiment_export.csv"; return result


@bp.route("/researcher/revisits.csv")
@researcher_auth
def revisits_csv():
    rows=get_db().execute("""SELECT p.participant_code,r.scenario_code,r.opened_at,r.closed_at,r.duration_ms,
        CASE rr.reason_code WHEN 'memory' THEN '没有完全记清地图中的位置或路径信息' WHEN 'reliability' THEN '希望进一步确认地图信息是否可靠' WHEN 'both' THEN '两方面都有' WHEN 'other' THEN '其他原因' END revisit_reason,
        rr.other_reason,rr.answered_at reason_answered_at
        FROM revisit_events r JOIN sessions s ON s.id=r.session_id JOIN participants p ON p.id=s.participant_id
        LEFT JOIN revisit_reason_responses rr ON rr.session_id=s.id ORDER BY r.id""").fetchall()
    out=io.StringIO(); writer=csv.writer(out); writer.writerow(["participant_code","scenario_code","opened_at","closed_at","duration_ms","revisit_reason","revisit_other_reason","reason_answered_at"]); writer.writerows([list(x) for x in rows])
    result=make_response("\ufeff"+out.getvalue()); result.headers["Content-Type"]="text/csv; charset=utf-8"; result.headers["Content-Disposition"]="attachment; filename=revisit_events.csv"; return result


@bp.route("/researcher/revisit-reasons.csv")
@researcher_auth
def revisit_reasons_csv():
    rows=get_db().execute("""SELECT p.participant_code,
        CASE rr.reason_code WHEN 'memory' THEN '没有完全记清地图中的位置或路径信息' WHEN 'reliability' THEN '希望进一步确认地图信息是否可靠' WHEN 'both' THEN '两方面都有' WHEN 'other' THEN '其他原因' END revisit_reason,
        rr.other_reason,rr.answered_at
        FROM revisit_reason_responses rr JOIN sessions s ON s.id=rr.session_id JOIN participants p ON p.id=s.participant_id
        ORDER BY rr.id""").fetchall()
    out=io.StringIO(); writer=csv.writer(out); writer.writerow(["participant_code","revisit_reason","revisit_other_reason","answered_at"]); writer.writerows([list(x) for x in rows])
    result=make_response("\ufeff"+out.getvalue()); result.headers["Content-Type"]="text/csv; charset=utf-8"; result.headers["Content-Disposition"]="attachment; filename=revisit_reasons.csv"; return result
