.PHONY: test
test:
	python3 -m py_compile moonlightos-launcher.py moonlightos_apps.py moonlightos_app_runner.py moonlightos_setup.py moonlightos_osk.py moonlightos_bluetooth.py moonlightos_display.py moonlightos_audio.py moonlightos_support.py moonlightos_rdp.py moonlightos_confirm.py moonlightos_controls.py gamepad-nav.py
	python3 -m unittest -v test_apps.py test_app_runner.py test_launcher.py test_setup.py test_osk.py test_bluetooth.py test_display.py test_audio.py test_gamepad_nav.py test_rdp.py test_confirm.py test_controls.py
