import testenv  # noqa: F401  (first: scratch run and state directories)
import unittest

import couchliteos_apps as apps
import couchliteos_home as home
import couchliteos_stream as stream
import couchliteos_tvscreens as tvscreens
import couchliteos_xmb as xmb

PC = stream.Host(name="Gaming-PC", uuid="UUID-1", apps=("Desktop", "Hades"))
NETFLIX = "--kiosk https://www.netflix.com/browse"


def app(app_id, kind="request", **fields):
    return apps.Application(id=app_id, name=app_id.upper(), kind=kind, **fields)


APPS = (
    app("moonlight", request="start-moonlight"),
    app("chiaki-ng", request="start-chiaki"),
    app("firefox", request="start-firefox"),
    app("netflix", kind="command", command="/usr/bin/firefox-esr", arguments=NETFLIX),
    app("terminal", kind="command", command="/bin/bash"),
    app("tailscale", request="start-tailscale"),
    app("work-pc", kind="rdp", connection="work-pc"),
)


class XmbTestCase(unittest.TestCase):
    def model(self, hosts=(PC,), applications=APPS, history=None, missing=(), values=None, power=None):
        self.world = {"hosts": list(hosts), "apps": tuple(applications), "history": dict(history or {})}
        home_model = home.HomeModel(
            hosts=lambda: list(self.world["hosts"]),
            applications=lambda: apps.LoadResult(self.world["apps"], ()),
            installed=lambda item: item.id not in missing,
            history=lambda: dict(self.world["history"]),
            network=lambda: "online",
        )
        settings = tvscreens.SettingsModel()
        settings.values = dict(values or {})
        return xmb.XmbModel(home_model, settings, power=power)

    @staticmethod
    def labels(model, key):
        return [item.label for item in model.categories[xmb.ORDER.index(key)].items]

    @staticmethod
    def items(model, key):
        return model.categories[xmb.ORDER.index(key)].items


class CategoriesTest(XmbTestCase):
    def test_categories_left_to_right_and_a_boot_lands_on_games(self):
        model = self.model()
        self.assertEqual([c.label for c in model.categories],
                         ["POWER", "SETTINGS", "TV & VIDEO", "GAMES", "APPS", "NETWORK"])
        self.assertEqual(model.category.key, xmb.GAMES)
        self.assertEqual(model.row, 0)

    def test_games_then_game_streaming_apps_then_gaming_pcs(self):
        model = self.model(history={("UUID-1", "Hades"): 5.0})
        self.assertEqual(self.labels(model, xmb.GAMES), ["HADES", "DESKTOP", "CHIAKI-NG", "GAMING PCS"])
        hades = self.items(model, xmb.GAMES)[0]
        self.assertEqual((hades.game, hades.played), (("GAMING-PC", "Hades"), 5.0))
        self.assertTrue(model.focus_last_played())
        self.assertEqual(model.focused().label, "HADES")

    def test_with_no_pc_games_offers_pairing_first(self):
        model = self.model(hosts=())
        self.assertEqual(self.labels(model, xmb.GAMES), [home.NO_GAMES, "CHIAKI-NG", "GAMING PCS"])
        self.assertEqual(model.activate(), ("hosts",))
        self.assertFalse(model.focus_last_played())

    def test_browsers_and_web_apps_are_tv_and_video(self):
        model = self.model()
        self.assertEqual(self.labels(model, xmb.VIDEO), ["FIREFOX", "NETFLIX"])
        netflix = self.items(model, xmb.VIDEO)[1]
        self.assertEqual((netflix.url, netflix.detail, netflix.app), ("https://www.netflix.com/browse", "WEB", "netflix"))
        self.assertEqual(netflix.action, ("app", "netflix"))

    def test_with_nothing_to_watch_tv_and_video_offers_to_add_some(self):
        model = self.model(applications=[a for a in APPS if a.id not in ("firefox", "netflix")])
        self.assertEqual(self.labels(model, xmb.VIDEO), [xmb.NO_VIDEO])
        self.assertEqual(self.items(model, xmb.VIDEO)[0].action, ("entry", tvscreens.SCREEN, "applications"))

    def test_moonlight_is_not_an_item_and_missing_apps_are_hidden(self):
        model = self.model(missing=("terminal",))
        everything = [item.app for category in model.categories for item in category.items]
        self.assertNotIn("moonlight", everything)
        self.assertNotIn("terminal", everything)
        self.assertEqual(self.labels(model, xmb.APPS), ["ADD AN APPLICATION"])

    def test_network_has_its_state_tailscale_and_remote_desktop_connections(self):
        model = self.model()
        self.assertEqual(self.labels(model, xmb.NETWORK),
                         ["INTERNET", "WI-FI AND WIRED", "TAILSCALE", "WORK-PC", "REMOTE DESKTOP"])
        self.assertEqual(self.items(model, xmb.NETWORK)[0].detail, "ONLINE")
        self.assertEqual(self.items(model, xmb.NETWORK)[3].icon, "desktop")

    def test_a_manifest_category_and_icon_win(self):
        moved = app("terminal", kind="command", command="/bin/bash", category="network", icon="terminal")
        model = self.model(applications=[moved])
        item = self.items(model, xmb.NETWORK)[2]
        self.assertEqual((item.label, item.icon), ("TERMINAL", "terminal"))

    def test_settings_show_values_and_skip_what_lives_elsewhere(self):
        model = self.model(values={"DISPLAY": "1920 X 1080 AT 60 HZ"})
        labels = self.labels(model, xmb.SETTINGS)
        self.assertNotIn("BACK", labels)
        self.assertNotIn("TAILSCALE", labels)
        self.assertNotIn("ACTIVE APPLICATIONS", labels)
        items = {item.label: item for item in self.items(model, xmb.SETTINGS)}
        self.assertEqual(items["DISPLAY"].detail, "1920 X 1080 AT 60 HZ")
        self.assertEqual(items["DISPLAY"].action, ("entry", tvscreens.SCREEN, "display"))
        self.assertEqual(items["AUDIO"].detail, "WHERE THE SOUND GOES AND A TEST TONE")
        self.assertEqual(items["CHECK FOR UPDATES"].action, ("updates",))
        self.assertEqual(items["SYSTEM DIAGNOSTICS"].action, ("entry", tvscreens.APP, "system-diagnostics"))

    def test_power_asks_and_sleep_is_off_where_unsupported(self):
        model = self.model(power=lambda: tvscreens.PowerModel(can_sleep=False, can_wake=False))
        items = self.items(model, xmb.POWER)
        self.assertEqual([item.label for item in items],
                         ["SLEEP: NOT SUPPORTED ON THIS PC", "RESTART", "SHUT DOWN", "ACTIVE APPLICATIONS"])
        self.assertIsNone(items[0].action)
        self.assertEqual(items[1].action, ("power", "reboot"))
        self.assertEqual(items[3].action, ("entry", tvscreens.VIEW, "active"))

    def test_a_broken_power_model_still_leaves_restart_and_shut_down(self):
        def broken():
            raise RuntimeError("logind is gone")
        labels = self.labels(self.model(power=broken), xmb.POWER)
        self.assertIn("RESTART", labels)
        self.assertIn("SHUT DOWN", labels)

    def test_web_url_only_for_command_apps(self):
        self.assertEqual(xmb.web_url(APPS[3]), "https://www.netflix.com/browse")
        self.assertEqual(xmb.web_url(app("x", request="start-x")), "")
        self.assertEqual(xmb.web_url(APPS[4]), "")

    def test_known_websites_by_host_or_parent_domain(self):
        self.assertEqual(xmb.web_service("https://www.netflix.com/browse"), "film")
        self.assertEqual(xmb.web_service("https://music.youtube.com/"), "music-note")
        self.assertEqual(xmb.web_service("https://www.youtube.com/tv"), "play-circle")
        self.assertEqual(xmb.web_service("HTTPS://TV.YOUTUBE.COM:443/"), "television")
        self.assertIsNone(xmb.web_service("https://netflix.com.example.org/"))
        self.assertIsNone(xmb.web_service("https://com/"))
        self.assertIsNone(xmb.web_service("not a link"))

    def test_websites_go_where_they_belong_with_our_icon(self):
        def web(app_id, url):
            return app(app_id, kind="command", command="/usr/bin/firefox-esr", arguments=f"--kiosk {url}")
        model = self.model(applications=APPS + (
            web("geforce-now", "https://play.geforcenow.com/"), web("gmail", "https://mail.google.com/"),
            web("somewhere", "https://example.org/"),
        ))
        self.assertIn("GEFORCE-NOW", self.labels(model, xmb.GAMES))
        self.assertIn("GMAIL", self.labels(model, xmb.APPS))
        self.assertIn("SOMEWHERE", self.labels(model, xmb.VIDEO))
        icons = {item.label: item.icon for category in model.categories for item in category.items}
        self.assertEqual((icons["NETFLIX"], icons["GEFORCE-NOW"], icons["GMAIL"], icons["SOMEWHERE"]),
                         ("film", "cloud-game", "envelope", "play-circle"))


class FocusTest(XmbTestCase):
    def test_nothing_wraps(self):
        model = self.model()
        for _ in range(3):
            self.assertEqual(model.move_h(-1), xmb.MOVED)
        self.assertEqual(model.category.key, xmb.POWER)
        self.assertEqual(model.move_h(-1), xmb.EDGE)
        self.assertEqual(model.move_v(-1), xmb.EDGE)
        for _ in range(5):
            model.move_h(1)
        self.assertEqual(model.category.key, xmb.NETWORK)
        self.assertEqual(model.move_h(1), xmb.EDGE)

    def test_each_category_remembers_its_item(self):
        model = self.model()
        model.move_v(1)
        model.move_v(1)  # GAMES: CHIAKI-NG
        model.move_h(-1)
        model.move_v(1)  # TV & VIDEO: NETFLIX
        model.move_h(1)
        self.assertEqual(model.focused().label, "CHIAKI-NG")
        model.move_h(-1)
        self.assertEqual(model.focused().label, "NETFLIX")
        self.assertEqual(model.move_v(1), xmb.EDGE)

    def test_reload_keeps_the_focused_item_when_it_moves(self):
        model = self.model()
        model.move_h(1)
        model.move_v(1)
        focused = model.focused().key
        self.world["apps"] = (app("ssh", kind="command", command="/usr/bin/ssh"), *self.world["apps"])
        model.reload()
        self.assertEqual(model.focused().key, focused)

    def test_reload_when_the_focused_item_is_gone_stays_in_range(self):
        model = self.model()
        model.move_h(-1)
        model.move_v(1)  # NETFLIX
        self.world["apps"] = tuple(a for a in APPS if a.id not in ("firefox", "netflix"))
        model.reload()
        self.assertEqual(model.category.key, xmb.VIDEO)
        self.assertEqual(model.focused().label, xmb.NO_VIDEO)

    def test_back_goes_to_the_top_then_to_games(self):
        model = self.model()
        model.move_h(-1)
        model.move_v(1)
        self.assertEqual(model.back(), xmb.MOVED)
        self.assertEqual(model.row, 0)
        self.assertEqual(model.back(), xmb.MOVED)
        self.assertEqual(model.category.key, xmb.GAMES)
        self.assertEqual(model.back(), xmb.STILL)

    def test_new_settings_values_keep_the_focus(self):
        model = self.model()
        model.move_h(-1)
        model.move_h(-1)
        model.move_v(1)
        model.settings_values({"APPEARANCE": "MIDNIGHT"})
        self.assertEqual(model.focused().label, "APPEARANCE")
        self.assertEqual(model.focused().detail, "MIDNIGHT")

    def test_activate_returns_the_items_action(self):
        model = self.model(history={("UUID-1", "Desktop"): 9.0})
        action = model.activate()
        self.assertEqual((action[0], action[2]), ("stream", "Desktop"))


class CacheTest(unittest.TestCase):
    def test_kept_makes_a_value_once_and_drops_the_least_recently_used(self):
        kept, made = xmb.Kept(2), []

        def make(key):
            return lambda: made.append(key) or key.upper()

        self.assertEqual(kept.get("a", make("a")), "A")
        self.assertEqual(kept.get("a", make("a")), "A")
        kept.get("b", make("b"))
        kept.get("a", make("a"))  # a is now the most recent
        kept.get("c", make("c"))  # b goes
        kept.get("a", make("a"))
        kept.get("b", make("b"))
        self.assertEqual(made, ["a", "b", "c", "b"])
        self.assertLessEqual(len(kept.items), 2)
        kept.clear()
        kept.get("a", make("a"))
        self.assertEqual(made[-1], "a")

    def test_fresh_makes_again_only_after_its_seconds(self):
        now = [10.0]
        fresh, made = xmb.Fresh(1.5, clock=lambda: now[0]), []
        make = lambda: made.append(now[0]) or len(made)  # noqa: E731
        self.assertEqual(fresh.get("status", make), 1)
        now[0] = 11.0
        self.assertEqual(fresh.get("status", make), 1)
        self.assertEqual(fresh.get("other", make), 2)  # a new key: made at once
        now[0] = 11.6
        self.assertEqual(fresh.get("status", make), 3)
        fresh.clear()
        self.assertEqual(fresh.get("status", make), 4)

    def test_fresh_stays_small(self):
        fresh = xmb.Fresh(5, clock=lambda: 0.0, size=3)
        for key in range(10):
            fresh.get(key, lambda: 0)
        self.assertLessEqual(len(fresh.items), 3)


if __name__ == "__main__":
    unittest.main()
