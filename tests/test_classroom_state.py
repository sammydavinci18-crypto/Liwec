"""Run with:  python -m unittest discover -s tests   (no server or database needed)"""
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import classroom_state as cs  # noqa: E402


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class InkOps(unittest.TestCase):
    def test_line_is_cleaned(self):
        op = cs.clean_ink_op({"t": "line", "id": "abc", "k": "hi", "c": "#ff0000", "w": 9, "p": [[2, -1], [0.5, 0.25]]})
        self.assertEqual(op["k"], "hi")
        self.assertEqual(op["w"], 0.08)                          # clamped
        self.assertEqual(op["p"], [[1, 0], [0.5, 0.25]])         # clamped to the board

    def test_bad_values_fall_back(self):
        op = cs.clean_ink_op({"t": "line", "id": 5, "k": "laser", "c": "red", "w": "x", "p": [[0, 0]]})
        self.assertEqual((op["k"], op["c"], op["w"]), ("pen", "#111111", 0.006))

    def test_rejects_junk(self):
        for bad in (None, 3, {}, {"t": "nope"}, {"t": "line", "p": []}, {"t": "line", "p": "x"},
                    {"t": "shape", "k": "star", "a": [0, 0], "b": [1, 1]},
                    {"t": "shape", "k": "rect", "a": [0, 0]},
                    {"t": "text", "s": "   "}):
            self.assertIsNone(cs.clean_ink_op(bad), bad)

    def test_nan_does_not_get_through(self):
        op = cs.clean_ink_op({"t": "text", "s": "hi", "x": float("nan"), "y": 0.2})
        self.assertEqual(op["x"], 0)

    def test_allowed_set(self):
        shape = {"t": "shape", "k": "rect", "a": [0, 0], "b": [1, 1]}
        self.assertIsNotNone(cs.clean_ink_op(shape, cs.STUDENT_OPS))
        self.assertIsNone(cs.clean_ink_op({"t": "clear"}, cs.STUDENT_OPS))
        self.assertIsNone(cs.clean_ink_op({"t": "undo"}, cs.STUDENT_OPS))
        self.assertIsNone(cs.clean_ink_op({"t": "text", "s": "a"}, cs.SLIDE_OPS))

    def test_images(self):
        ok = "data:image/jpeg;base64,/9j/4AAQSkZJRg=="
        self.assertIsNotNone(cs.clean_ink_op({"t": "image", "id": "i", "src": ok, "x": 0.1, "y": 0.1, "w": 0.3, "h": 0.2}))
        for bad in ("http://evil.example/x.png", "data:text/html;base64,PHNjcmlwdD4=",
                    "data:image/svg+xml;base64,PHN2Zz4=", "data:image/png;base64,AAAA\"onerror=\"x",
                    "data:image/png;base64," + "A" * (cs.MAX_IMAGE_CHARS + 1)):
            self.assertIsNone(cs.clean_ink_op({"t": "image", "src": bad}), bad[:40])


class BoardTests(unittest.TestCase):
    def line(self, i="a", pts=None):
        return cs.clean_ink_op({"t": "line", "id": i, "c": "#111111", "w": 0.006, "p": pts or [[0.1, 0.1]]})

    def test_more_extends_the_line(self):
        b = cs.Board()
        self.assertTrue(b.apply(self.line()))
        self.assertTrue(b.apply(cs.clean_ink_op({"t": "more", "id": "a", "p": [[0.2, 0.2], [0.3, 0.3]]})))
        self.assertEqual(len(b.ops), 1)
        self.assertEqual(len(b.ops[0]["p"]), 3)

    def test_more_without_a_line_is_ignored(self):
        b = cs.Board()
        self.assertFalse(b.apply(cs.clean_ink_op({"t": "more", "id": "zzz", "p": [[0, 0]]})))

    def test_undo_removes_one_whole_stroke(self):
        b = cs.Board()
        b.apply(self.line("a"))
        b.apply(self.line("b"))
        b.apply(cs.clean_ink_op({"t": "more", "id": "b", "p": [[0.5, 0.5]]}))
        b.apply({"t": "undo"})
        self.assertEqual([o["id"] for o in b.ops], ["a"])
        b.apply({"t": "undo"})
        self.assertFalse(b.apply({"t": "undo"}))                  # nothing left: not relayed

    def test_pages_keep_their_own_content_and_paper(self):
        b = cs.Board()
        b.apply(self.line("a"))
        self.assertTrue(b.set_bg("grid"))
        self.assertTrue(b.add_page())
        self.assertEqual((b.page, b.bg, b.ops), (1, "grid", []))  # a new page keeps the paper
        b.apply(self.line("b"))
        self.assertTrue(b.go_to(0))
        self.assertEqual([o["id"] for o in b.ops], ["a"])
        self.assertFalse(b.go_to(5))
        self.assertFalse(b.go_to("x"))
        self.assertFalse(b.set_bg("sparkles"))

    def test_page_limit(self):
        b = cs.Board()
        for _ in range(cs.Board.MAX_PAGES + 5):
            b.add_page()
        self.assertEqual(len(b.pages), cs.Board.MAX_PAGES)

    def test_image_limit_is_per_board(self):
        b = cs.Board()
        img = {"t": "image", "id": "i", "src": "data:image/png;base64,AAAA", "x": 0, "y": 0, "w": 0.2, "h": 0.2}
        for i in range(cs.MAX_IMAGES_PER_BOARD):
            self.assertTrue(b.apply(dict(img, id=str(i))))
        b.add_page()
        self.assertFalse(b.apply(dict(img, id="over")))

    def test_closing_the_board_takes_writing_rights_away(self):
        b = cs.Board()
        b.set_open(True)
        b.set_writer("sid1", "Amina")
        self.assertEqual(b.payload()["writer"]["name"], "Amina")
        b.set_open(False)
        self.assertIsNone(b.writer)

    def test_every_change_bumps_the_version(self):
        b = cs.Board()
        seen = [b.v]
        b.apply(self.line()); seen.append(b.v)
        b.set_bg("lines"); seen.append(b.v)
        b.set_ruler({"on": True, "x": 0.5, "y": 0.5, "a": 1, "l": 0.5}); seen.append(b.v)
        self.assertEqual(seen, sorted(set(seen)))

    def test_ruler_is_clamped_and_can_be_turned_off(self):
        b = cs.Board()
        b.set_ruler({"on": True, "x": 9, "y": -2, "a": 99, "l": 50})
        self.assertEqual((b.ruler["x"], b.ruler["y"], b.ruler["a"], b.ruler["l"]), (1, 0, 7, 1.2))
        b.set_ruler({"on": False})
        self.assertIsNone(b.ruler)
        self.assertFalse(b.set_ruler("x"))


class SlideInkTests(unittest.TestCase):
    def test_remembered_per_slide(self):
        ink = cs.SlideInk()
        op = cs.clean_ink_op({"t": "line", "id": "a", "p": [[0.1, 0.1]]}, cs.SLIDE_OPS)
        ink.apply(7, 0, op)
        self.assertEqual(len(ink.ops(7, 0)), 1)
        self.assertEqual(ink.ops(7, 1), [])
        self.assertEqual(len(ink.ops(8, 0)), 0)
        ink.apply(7, 0, {"t": "clear"})
        self.assertEqual(ink.ops(7, 0), [])

    def test_drop_deck(self):
        ink = cs.SlideInk()
        ink.apply(7, 0, cs.clean_ink_op({"t": "line", "id": "a", "p": [[0, 0]]}))
        ink.apply(8, 0, cs.clean_ink_op({"t": "line", "id": "a", "p": [[0, 0]]}))
        ink.drop_deck(7)
        self.assertEqual(ink.ops(7, 0), [])
        self.assertEqual(len(ink.ops(8, 0)), 1)


class PollTests(unittest.TestCase):
    def test_counts_and_changing_your_answer(self):
        p = cs.Poll(1, "Which?", 4)
        p.answer("s1", 0); p.answer("s2", 2); p.answer("s3", 2)
        self.assertEqual(p.counts(), [1, 0, 2, 0])
        p.answer("s3", 1)                                         # one vote each: changing replaces
        self.assertEqual(p.counts(), [1, 1, 1, 0])

    def test_invalid_answers(self):
        p = cs.Poll(1, "", 3)
        self.assertFalse(p.answer("s", 3))
        self.assertFalse(p.answer("s", -1))
        self.assertFalse(p.answer("s", "x"))
        self.assertFalse(p.answer("s", None))
        self.assertEqual(p.answers, {})

    def test_closed_poll_takes_no_answers(self):
        p = cs.Poll(1, "", 4)
        p.answer("s1", 1)
        p.close()
        self.assertFalse(p.answer("s1", 2))
        self.assertFalse(p.answer("s2", 2))
        self.assertEqual(p.counts(), [0, 1, 0, 0])

    def test_option_count_is_limited(self):
        self.assertEqual(cs.Poll(1, "", 99).n, 6)
        self.assertEqual(cs.Poll(1, "", 1).n, 2)

    def test_the_right_answer_stays_secret_until_revealed(self):
        p = cs.Poll(1, "2+2?", 4, correct=1)
        p.answer("s1", 1)
        self.assertIsNone(p.public_view("s1")["correct"])
        self.assertIsNone(p.public_view("s1")["counts"])
        self.assertEqual(p.host_view({"s1": "A"})["correct"], 1)
        p.reveal()
        v = p.public_view("s1")
        self.assertEqual((v["correct"], v["open"], v["counts"]), (1, False, [0, 1, 0, 0]))

    def test_correct_out_of_range_is_dropped(self):
        self.assertIsNone(cs.Poll(1, "", 3, correct=5).correct)
        self.assertIsNone(cs.Poll(1, "", 3, correct="1").correct)

    def test_host_sees_who_is_missing(self):
        p = cs.Poll(1, "", 4)
        p.answer("s1", 0)
        v = p.host_view({"s1": "Zed", "s2": "Amina", "s3": "Bo"})
        self.assertEqual((v["answered"], v["total"], v["waiting"]), (1, 3, ["Amina", "Bo"]))

    def test_sharing_results_with_students(self):
        p = cs.Poll(1, "", 2)
        p.answer("s1", 0)
        self.assertIsNone(p.public_view("s1")["counts"])
        p.shared = True
        self.assertEqual(p.public_view("s1")["counts"], [1, 0])
        self.assertEqual(p.public_view("s1")["mine"], 0)
        self.assertIsNone(p.public_view("nobody")["mine"])


class BreakoutTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.b = cs.Breakouts(clock=self.clock, rng=random.Random(1))
        self.sids = ["s%d" % i for i in range(1, 8)]

    def test_groups_are_even_and_everyone_is_placed_once(self):
        self.assertTrue(self.b.start(self.sids, "groups", 3))
        sizes = sorted(len(g["members"]) for g in self.b.groups)
        self.assertEqual(sizes, [2, 2, 3])
        placed = [s for g in self.b.groups for s in g["members"]]
        self.assertEqual(sorted(placed), sorted(self.sids))

    def test_pairs_leave_no_one_alone(self):
        self.assertTrue(self.b.start(self.sids, "pairs", 0))       # 7 students: 2 pairs + a trio
        self.assertEqual(sorted(len(g["members"]) for g in self.b.groups), [2, 2, 3])
        self.assertTrue(all(g["name"].startswith("Pair") for g in self.b.groups))

    def test_cannot_make_more_groups_than_students(self):
        self.assertTrue(self.b.start(["a", "b", "c"], "groups", 10))
        self.assertEqual(len(self.b.groups), 3)

    def test_needs_two_students(self):
        self.assertFalse(self.b.start(["a"], "pairs", 0))
        self.assertFalse(self.b.start([], "groups", 2))
        self.assertFalse(self.b.active)

    def test_move_and_remove(self):
        self.b.start(self.sids, "groups", 2)
        g1, g2 = self.b.groups
        sid = g1["members"][0]
        self.assertTrue(self.b.move(sid, g2["id"]))
        self.assertEqual(self.b.group_of(sid)["id"], g2["id"])
        self.assertFalse(self.b.move(sid, 99))
        self.b.remove(sid)
        self.assertIsNone(self.b.group_of(sid))

    def test_late_arrival_goes_to_the_smallest_group(self):
        self.b.start(["a", "b", "c", "d", "e"], "groups", 2)       # 3 and 2
        small = min(self.b.groups, key=lambda g: len(g["members"]))
        g = self.b.add_late("late")
        self.assertEqual(g["id"], small["id"])
        self.assertEqual(self.b.add_late("late")["id"], small["id"])  # not added twice

    def test_bringing_everyone_back(self):
        self.b.start(self.sids, "groups", 2, minutes=5)
        self.b.end()
        self.assertFalse(self.b.active)
        self.assertEqual(self.b.groups, [])
        self.assertIsNone(self.b.add_late("x"))

    def test_countdown_and_extending(self):
        self.b.start(self.sids, "groups", 2, minutes=5)
        self.assertEqual(self.b.seconds_left(), 300)
        self.clock.t += 100
        self.assertEqual(self.b.seconds_left(), 200)
        self.b.extend(2)
        self.assertEqual(self.b.seconds_left(), 320)
        self.clock.t += 1000
        self.assertEqual(self.b.seconds_left(), 0)

    def test_no_minutes_means_until_ended(self):
        self.b.start(self.sids, "groups", 2)
        self.assertIsNone(self.b.seconds_left())

    def test_students_only_hear_their_own_group_unless_teacher_is_there(self):
        self.b.start(self.sids, "groups", 2)
        g1, g2 = self.b.groups
        member = g1["members"][0]
        names = {s: s.upper() for s in self.sids}
        v = self.b.student_view(member, names)
        self.assertEqual(set(v["mate_sids"]), set(g1["members"]) - {member})
        self.assertFalse(v["teacher_here"])
        self.b.set_visit(g1["id"])
        self.assertTrue(self.b.student_view(member, names)["teacher_here"])
        other = g2["members"][0]
        self.assertFalse(self.b.student_view(other, names)["teacher_here"])
        self.b.set_visit("all")
        self.assertTrue(self.b.student_view(other, names)["teacher_here"])
        self.assertFalse(self.b.set_visit(99))

    def test_student_view_when_not_in_a_room(self):
        self.assertEqual(self.b.student_view("x", {}), {"active": False})


class TimerTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.t = cs.Timer(clock=self.clock)

    def test_counts_down(self):
        self.t.start(120, "Q1 to Q5")
        self.assertEqual(self.t.payload()["remaining"], 120)
        self.clock.t += 30
        p = self.t.payload()
        self.assertEqual((p["remaining"], p["label"], p["total"]), (90, "Q1 to Q5", 120))

    def test_limits(self):
        self.t.start(1)
        self.assertEqual(self.t.total, cs.Timer.MIN_SECONDS)
        self.t.start(10 ** 9)
        self.assertEqual(self.t.total, cs.Timer.MAX_SECONDS)

    def test_pause_and_resume_keep_the_time(self):
        self.t.start(100)
        self.clock.t += 40
        self.assertTrue(self.t.pause())
        self.clock.t += 500
        self.assertEqual(self.t.payload()["remaining"], 60)
        self.assertTrue(self.t.payload()["paused"])
        self.assertTrue(self.t.resume())
        self.clock.t += 10
        self.assertEqual(self.t.payload()["remaining"], 50)

    def test_adding_time(self):
        self.t.start(60)
        self.clock.t += 50
        self.t.add(120)
        self.assertEqual(self.t.payload()["remaining"], 130)
        self.assertGreaterEqual(self.t.total, 130)             # the bar never runs past full

    def test_cannot_go_below_zero_by_subtracting(self):
        self.t.start(60)
        self.t.add(-3000)
        self.assertGreater(self.t.payload()["remaining"], 0)

    def test_time_up_lingers_then_disappears(self):
        self.t.start(10)
        self.clock.t += 11
        p = self.t.payload()
        self.assertTrue(p["active"])
        self.assertEqual(p["remaining"], 0)
        self.clock.t += cs.Timer.LINGER
        self.assertFalse(self.t.payload()["active"])
        self.assertFalse(self.t.add(30))                       # too late to revive it

    def test_stop(self):
        self.t.start(60)
        self.t.stop()
        self.assertEqual(self.t.payload(), {"active": False})


class BreakTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.b = cs.Break(clock=self.clock)

    def test_minutes_become_a_countdown(self):
        self.b.start(5, "Stretch and drink water")
        p = self.b.payload()
        self.assertEqual((p["active"], p["remaining"], p["total"]), (True, 300, 300))
        self.assertEqual(p["message"], "Stretch and drink water")
        self.clock.t += 100
        self.assertEqual(self.b.payload()["remaining"], 200)

    def test_one_minute_and_custom_minutes(self):
        self.b.start(1)
        self.assertEqual(self.b.total, 60)
        self.b.start(7.5)
        self.assertEqual(self.b.total, 450)

    def test_limits_and_junk_input(self):
        self.b.start(0)
        self.assertEqual(self.b.total, cs.Break.MIN_SECONDS)
        self.b.start(10 ** 6)
        self.assertEqual(self.b.total, cs.Break.MAX_SECONDS)
        self.b.start("abc")
        self.assertEqual(self.b.total, 300)                    # falls back to 5 minutes

    def test_message_is_trimmed(self):
        self.b.start(5, "  " + "x" * 500)
        self.assertEqual(len(self.b.message), 200)

    def test_extend_and_end(self):
        self.b.start(2)
        self.clock.t += 60
        self.assertTrue(self.b.add(120))
        self.assertEqual(self.b.payload()["remaining"], 180)
        self.b.end()
        self.assertEqual(self.b.payload(), {"active": False})
        self.assertFalse(self.b.add(60))

    def test_back_message_lingers_then_disappears(self):
        self.b.start(1)
        self.clock.t += 61
        p = self.b.payload()
        self.assertTrue(p["active"])
        self.assertEqual(p["remaining"], 0)
        self.assertFalse(self.b.add(60))                       # too late to extend
        self.clock.t += cs.Break.LINGER
        self.assertFalse(self.b.payload()["active"])

    def test_a_room_has_its_own_break_and_timer(self):
        cs.drop("ROOMB")
        t = cs.tools("ROOMB")
        t.timer.start(60)
        t.rest.start(5)
        self.assertTrue(t.timer.payload()["active"])
        t.rest.end()
        self.assertTrue(t.timer.payload()["active"])
        cs.drop("ROOMB")


class RegistryTests(unittest.TestCase):
    def test_lifecycle(self):
        cs.drop("ROOM1")
        self.assertIsNone(cs.peek("ROOM1"))
        t = cs.tools("ROOM1")
        self.assertIs(cs.tools("ROOM1"), t)
        cs.drop("ROOM1")
        self.assertIsNone(cs.peek("ROOM1"))


if __name__ == "__main__":
    unittest.main()
