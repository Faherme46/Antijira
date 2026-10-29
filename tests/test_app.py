import copy
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from app import make_handler
from antijira.calendar_co import holidays, business_end, next_business_day
from antijira.domain import AppError, COLUMNS, parameters, validate_task, next_code, normalize_date, column_positions
from antijira.service import TaskService
from antijira.sheets import SheetsStore, revision
from antijira.locking import InstanceLock

CATEGORIES = [
    {"id":"desarrollo","name":"Desarrollo","worksheet":"PRIORIDADES DESARROLLO","code_prefix":"DS-","code_digits":3},
    {"id":"errores","name":"Errores","worksheet":"PRIORIDADES ERRORES","code_prefix":"ER-","code_digits":3},
]


def payload(category="desarrollo"):
    config = parameters()
    return {"category":category, "name":"Implementar registro", "module":"CARTERA",
            "description":"Detalle de la tarea", "requirements":"Criterios de aceptación",
            "assignee":config["people"][0], "priority":"5,5", "complexity":config["complexities"][0],
            "status":config["statuses"][0], "estimated_days":2, "planned_start":"2026-10-30",
            "end_mode":"auto","work_saturdays":False}


class MemoryStore:
    """Solo pruebas: simula filas de dos pestañas, sin persistencia local."""
    def __init__(self):
        self.lock = threading.RLock()
        self.rows = {c["id"]:[] for c in CATEGORIES}

    def categories(self):
        return copy.deepcopy(CATEGORIES)

    def category(self, category_id):
        found = next((c for c in self.categories() if c["id"] == category_id), None)
        if not found:
            raise AppError("Categoría inexistente",404)
        return found

    def read(self, category=None):
        category = category or CATEGORIES[0]
        tasks = []
        for number, original in enumerate(self.rows[category["id"]],2):
            task=copy.deepcopy(original)
            raw=[task.get(k,"") for k in COLUMNS]
            task.update(category=category["id"],category_name=category["name"],row=number,revision=revision(raw),_raw=raw)
            tasks.append(task)
        return tasks,list(COLUMNS.values()),len(tasks)+2,[]

    def prepare(self, category):
        pass

    def append(self, task, headers, next_row, category=None):
        self.rows[(category or CATEGORIES[0])["id"]].append(copy.deepcopy(task))

    def update(self, task, fields, headers, row_number, category):
        for key in fields:
            self.rows[category["id"]][row_number-2][key]=task[key]

    def move(self, task, old, headers, category, destination, target_headers, target_row):
        self.rows[destination["id"]].append(copy.deepcopy(task))
        del self.rows[category["id"]][old["row"]-2]


class CalendarTests(unittest.TestCase):
    def test_initial_day_is_included(self):
        self.assertEqual(business_end("2026-10-26",1),"2026-10-26")

    def test_weekend_and_emiliani_holiday(self):
        self.assertEqual(business_end("2026-10-30",2),"2026-11-03")
        self.assertEqual(business_end("2026-11-01",1),"2026-11-03")

    def test_two_half_saturdays_accumulate_one_day(self):
        self.assertEqual(business_end("2026-10-19",11,True),"2026-10-31")
        self.assertEqual(business_end("2026-10-19",11,False),"2026-11-03")
        self.assertEqual(business_end("2026-10-24",1,True),"2026-10-26")

    def test_holy_week_and_cross_year(self):
        self.assertEqual(business_end("2026-04-01",2),"2026-04-06")
        self.assertEqual(business_end("2026-12-31",2),"2027-01-04")

    def test_holiday_saturday_is_not_counted(self):
        self.assertEqual(business_end("2027-12-25",1,True),"2027-12-27")

    def test_2026_official_holidays(self):
        expected={"2026-01-01","2026-01-12","2026-03-23","2026-04-02","2026-04-03",
                  "2026-05-01","2026-05-18","2026-06-08","2026-06-15","2026-06-29",
                  "2026-07-20","2026-08-07","2026-08-17","2026-10-12","2026-11-02",
                  "2026-11-16","2026-12-08","2026-12-25"}
        self.assertEqual({d.isoformat() for d in holidays(2026)},expected)

    def test_next_business_day_skips_weekend_and_holiday(self):
        self.assertEqual(next_business_day("2026-10-30"), "2026-11-03")


class DomainTests(unittest.TestCase):
    def test_priority_decimal_and_auto_end(self):
        task=validate_task(payload(),parameters())
        self.assertEqual(task["priority"],"5.5")
        self.assertEqual(task["estimated_days"],2)
        self.assertEqual(task["planned_end"],"2026-11-03")

    def test_invalid_fields(self):
        for replacement in ({"priority":"Alta"},{"priority":"NaN"},{"priority":True},{"priority":0},
                            {"estimated_days":"1.5"},{"estimated_days":0},{"estimated_days":True},
                            {"planned_start":"2026-02-30"},{"assignee":"unknown"}):
            with self.subTest(replacement=replacement),self.assertRaises(AppError):
                validate_task({**payload(),**replacement},parameters())

    def test_manual_override(self):
        task=validate_task({**payload(),"end_mode":"manual","planned_end":"2026-11-10"},parameters())
        self.assertEqual(task["planned_end"],"2026-11-10")

    def test_attachment_links_are_validated(self):
        task=validate_task({**payload(),"attachments":[{"name":"Diseño","link":"https://example.com/diseno"}]},parameters())
        self.assertEqual(task["attachments"],[{"name":"Diseño","link":"https://example.com/diseno"}])
        for link in ("javascript:alert(1)","archivo-local","ftp://example.com/file"):
            with self.subTest(link=link),self.assertRaises(AppError):
                validate_task({**payload(),"attachments":[{"name":"Inválido","link":link}]},parameters())

    def test_creation_does_not_accept_client_actual_dates(self):
        task=validate_task({**payload(),"actual_start":"2020-01-01","actual_end":"2020-01-01","code":"BAD"},parameters())
        self.assertEqual((task["actual_start"],task["actual_end"],task["code"]),("","",""))

    def test_increment_skips_legacy_blank_codes(self):
        self.assertEqual(next_code([{"code":"DS-124"},{"code":""},{"code":"DS-003"}],CATEGORIES[0]),"DS-125")
        with self.assertRaises(AppError):
            next_code([{"code":"DS-001"},{"code":"DS-001"}],CATEGORIES[0])

    def test_dates_and_original_column_aliases(self):
        self.assertEqual(normalize_date(46322),"2026-10-27")
        self.assertEqual(normalize_date("16/6/2026"),"2026-06-16")
        self.assertEqual(normalize_date(""),"")
        mapping=column_positions(["CÓDIGO","ERRORES","RELACIONADO","TIEMPO ESTIMADO (HORA)"])
        self.assertEqual(mapping,{"code":0,"name":1,"module":2})

    def test_partial_update_preserves_historical_data(self):
        old=validate_task(payload(),parameters())
        old.update(estimated_days="",complexity="",assignee="ANGEL",end_mode="manual")
        task=validate_task({"description":"Nuevo contexto"},parameters(),old)
        self.assertEqual(task["estimated_days"],"")
        self.assertEqual(task["assignee"],"ANGEL")
        self.assertEqual(task["description"],"Nuevo contexto")


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.store=MemoryStore()
        self.service=TaskService(self.store)

    def current(self, category=CATEGORIES[0]):
        return self.store.read(category)[0][0]

    def test_independent_sequences_under_concurrency(self):
        with ThreadPoolExecutor(max_workers=12) as pool:
            tasks=list(pool.map(lambda n:self.service.create_task(payload(CATEGORIES[n%2]["id"])),range(80)))
        self.assertEqual(len({t["code"] for t in tasks}),80)
        self.assertEqual(sorted(t["code"] for t in tasks if t["category"]=="desarrollo"),[f"DS-{n:03}" for n in range(1,41)])
        self.assertEqual(sorted(t["code"] for t in tasks if t["category"]=="errores"),[f"ER-{n:03}" for n in range(1,41)])

    def test_numeric_order(self):
        for priority in ("6.2","1","5,5","2","6"):
            self.service.create_task({**payload(),"priority":priority})
        self.assertEqual([t["priority"] for t in self.service.list_tasks()["tasks"]],["1","2","5.5","6","6.2"])

    def test_transitions_stamp_dates_and_allow_manual_correction(self):
        self.service.create_task(payload())
        task=self.current()
        with patch("antijira.service.today_co",return_value="2026-10-27"):
            self.service.update_task("desarrollo",2,{"revision":task["revision"],"status":"EN CURSO"})
        task=self.current()
        self.assertEqual(task["actual_start"],"2026-10-27")
        with patch("antijira.service.today_co",return_value="2026-10-28"):
            self.service.update_task("desarrollo",2,{"revision":task["revision"],"status":"TERMINADO"})
        task=self.current()
        self.assertEqual(task["actual_end"],"2026-10-28")
        self.service.update_task("desarrollo",2,{"revision":task["revision"],"actual_start":"2026-10-26","actual_end":"2026-10-29"})
        task=self.current()
        self.assertEqual(task["actual_start"],"2026-10-26")
        self.service.update_task("desarrollo",2,{"revision":task["revision"],"status":"EN CURSO"})
        self.assertEqual(self.current()["actual_end"],"2026-10-29")

    def test_explicit_empty_real_date_wins(self):
        self.service.create_task(payload())
        task=self.current()
        self.service.update_task("desarrollo",2,{"revision":task["revision"],"status":"EN CURSO","actual_start":""})
        self.assertEqual(self.current()["actual_start"],"")

    def test_stale_edit_is_rejected(self):
        self.service.create_task(payload())
        old=self.current()
        self.service.update_task("desarrollo",2,{"revision":old["revision"],"name":"Cambio de otro usuario"})
        with self.assertRaises(AppError) as error:
            self.service.update_task("desarrollo",2,{"revision":old["revision"],"name":"Edición antigua"})
        self.assertEqual(error.exception.status,409)

    def test_legacy_code_assigned_on_edit(self):
        self.service.create_task(payload("errores"))
        self.store.rows["errores"][0]["code"]=""
        task=self.current(CATEGORIES[1])
        updated=self.service.update_task("errores",2,{"revision":task["revision"],"description":"Corregido"})
        self.assertEqual(updated["code"],"ER-001")

    def test_category_transfer_gets_destination_code(self):
        self.service.create_task(payload())
        self.service.create_task(payload("errores"))
        task=self.current()
        updated=self.service.update_task("desarrollo",2,{"revision":task["revision"],"category":"errores"})
        self.assertEqual(updated["code"],"ER-002")
        self.assertEqual(len(self.store.rows["desarrollo"]),0)
        self.assertEqual(len(self.store.rows["errores"]),2)

    def test_duration_update_recalculates_end(self):
        self.service.create_task(payload())
        task=self.current()
        updated=self.service.update_task("desarrollo",2,{"revision":task["revision"],"estimated_days":1})
        self.assertEqual(updated["planned_end"],"2026-10-30")

    def test_actual_dates_cannot_be_reversed(self):
        self.service.create_task(payload())
        with self.assertRaises(AppError):
            self.service.update_task("desarrollo",2,{"revision":self.current()["revision"],"actual_start":"2026-10-29","actual_end":"2026-10-27"})

    def test_single_server_lock(self):
        with tempfile.TemporaryDirectory() as folder,patch("antijira.locking.ROOT",Path(folder)):
            with InstanceLock():
                with self.assertRaises(AppError):
                    with InstanceLock(): pass
            with InstanceLock(): pass


class SheetsTests(unittest.TestCase):
    def test_legacy_error_headers_and_extra_columns(self):
        headers=["CÓDIGO","ERRORES","RELACIONADO","PRIORIDAD","TIEMPO","responsable","fecha inicio","TIEMPO ESTIMADO (HORA)","Fecha entrega","Estado"]
        row=["","Error histórico","PAGOS",6,"MEDIO","ANGEL","2026-09-10",24,"2026-09-15","TERMINADO"]
        store=SheetsStore()
        with patch.object(store,"_request",return_value={"values":[headers,row]}):
            tasks,actual_headers,next_row,warnings=store.read(CATEGORIES[1])
        self.assertEqual(tasks[0]["estimated_days"],"")
        self.assertEqual(tasks[0]["module"],"PAGOS")
        self.assertEqual(tasks[0]["_raw"][7],24)
        self.assertEqual(tasks[0]["end_mode"],"manual")
        self.assertEqual(actual_headers,headers)
        self.assertEqual(next_row,3)
        self.assertTrue(warnings)

    def test_append_has_dynamic_columns_and_raw_input(self):
        store=SheetsStore()
        task=validate_task(payload(),parameters())
        headers=list(reversed(list(COLUMNS.values())))
        with patch.object(store,"_request",return_value={}) as request:
            store.append(task,headers,9,CATEGORIES[0])
        self.assertEqual(request.call_args.args[1],"A9:P9")
        self.assertEqual(request.call_args.args[3],{"valueInputOption":"RAW","insertDataOption":"INSERT_ROWS"})

    def test_prepare_only_adds_missing_headers_without_overwriting(self):
        store=SheetsStore()
        headers=list(COLUMNS.values())[:12]
        meta={"PRIORIDADES DESARROLLO":{"sheetId":1,"gridProperties":{"columnCount":33,"rowCount":100}}}
        with patch.object(store,"metadata",return_value=meta),patch.object(store,"read",return_value=([],headers,3,[])),patch.object(store,"_request",return_value={"values":[headers,["DS-001","Original"]]}),patch.object(store,"_api") as api:
            store.prepare(CATEGORIES[0])
        requests=api.call_args.args[2]["requests"]
        self.assertEqual(len(requests),1)
        self.assertEqual(requests[0]["updateCells"]["start"],{"sheetId":1,"rowIndex":0,"columnIndex":12})

    def test_update_only_touches_changed_cells(self):
        store=SheetsStore()
        task=validate_task(payload(),parameters())
        with patch.object(store,"_api") as api:
            store.update(task,["description"],list(COLUMNS.values()),2,CATEGORIES[0])
        data=api.call_args.args[2]["data"]
        self.assertEqual(len(data),1)
        self.assertEqual(data[0]["range"],"'PRIORIDADES DESARROLLO'!M2")

    def test_move_is_one_atomic_request_and_preserves_extra_formulas(self):
        store=SheetsStore()
        headers=list(COLUMNS.values())+["Extra"]
        old=validate_task(payload(),parameters())
        old.update(row=2,_raw=[old[k] for k in COLUMNS]+["calculated"],code="DS-001")
        task={**old,"code":"ER-001"}
        meta={c["worksheet"]:{"sheetId":i,"gridProperties":{"rowCount":100}} for i,c in enumerate(CATEGORIES,1)}
        with patch.object(store,"metadata",return_value=meta),patch.object(store,"_api") as api:
            store.move(task,old,headers,CATEGORIES[0],CATEGORIES[1],headers,5)
        self.assertEqual(api.call_count,1)
        requests=api.call_args.args[2]["requests"]
        self.assertIn("deleteDimension",requests[-1])
        self.assertTrue(any("copyPaste" in r for r in requests))

    def test_move_refuses_to_lose_extra_columns(self):
        store=SheetsStore()
        old=validate_task(payload(),parameters())
        old.update(row=2,_raw=[old[k] for k in COLUMNS]+["no perder"])
        meta={c["worksheet"]:{"sheetId":i,"gridProperties":{"rowCount":100}} for i,c in enumerate(CATEGORIES,1)}
        with patch.object(store,"metadata",return_value=meta),patch.object(store,"_api") as api:
            with self.assertRaises(AppError):
                store.move(old,old,list(COLUMNS.values())+["Extra"],CATEGORIES[0],CATEGORIES[1],list(COLUMNS.values()),5)
        api.assert_not_called()


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store=MemoryStore()
        cls.server=ThreadingHTTPServer(("127.0.0.1",0),make_handler(TaskService(cls.store)))
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True)
        cls.thread.start()
        cls.url=f"http://127.0.0.1:{cls.server.server_port}"
        with urlopen(cls.url+"/api/config") as response:
            cls.token=json.load(response)["csrf_token"]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def request(self,path,body,method="POST"):
        return urlopen(Request(self.url+path,json.dumps(body).encode(),{"Content-Type":"application/json","X-CSRF-Token":self.token},method=method))

    def test_create_then_edit(self):
        with self.request("/api/tasks",payload()) as response:
            self.assertEqual(response.status,201)
        task=self.store.read(CATEGORIES[0])[0][-1]
        with self.request(f"/api/tasks/desarrollo/{task['row']}",{"revision":task["revision"],"priority":"6,2"},"PUT") as response:
            self.assertEqual(json.load(response)["task"]["priority"],"6.2")

    def test_schedule_endpoint(self):
        with self.request("/api/schedule",{"planned_start":"2026-10-30","estimated_days":2,"work_saturdays":False}) as response:
            self.assertEqual(json.load(response)["planned_end"],"2026-11-03")

    def test_private_files_not_served(self):
        for path in ("/config/google_sheets.json","/.antijira/service-account.json","/app.py","/static/../app.py"):
            with self.subTest(path=path),self.assertRaises(HTTPError) as error:
                urlopen(self.url+path)
            self.assertEqual(error.exception.code,404)

    def test_csrf_required_for_edits(self):
        with self.assertRaises(HTTPError) as error:
            urlopen(Request(self.url+"/api/tasks/desarrollo/2",b"{}",{"Content-Type":"application/json"},method="PUT"))
        self.assertEqual(error.exception.code,403)

    def test_assets(self):
        for path in ("/","/static/app.js","/static/styles.css","/static/styles-v2.css"):
            with urlopen(self.url+path) as response:
                self.assertEqual(response.status,200)


if __name__ == "__main__":
    unittest.main()
