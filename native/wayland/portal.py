"""XDG ScreenCast v2+ D-Bus transport and one-shot authorization state machine."""

import json
import os
import sys
from uuid import uuid4

from . import CaptureError

SERVICE = "org.freedesktop.portal.Desktop"
PATH = "/org/freedesktop/portal/desktop"
SCREENCAST = "org.freedesktop.portal.ScreenCast"


class Portal:
    def __init__(self, *, diagnostic=None):
        self.diagnostic = diagnostic or (
            lambda value: print(json.dumps(value), file=sys.stderr, flush=True)
        )
        try:
            import dbus
            from dbus.mainloop.glib import DBusGMainLoop
            from gi.repository import GLib

            DBusGMainLoop(set_as_default=True)
            self.dbus, self.GLib = dbus, GLib
            self.bus = dbus.SessionBus()
            proxy = self.bus.get_object(SERVICE, PATH)
            self.interface = dbus.Interface(proxy, SCREENCAST)
            self.properties = dict(
                dbus.Interface(proxy, "org.freedesktop.DBus.Properties").GetAll(
                    SCREENCAST
                )
            )
        except (ImportError, ValueError):
            raise CaptureError("portal_dependencies_unavailable") from None
        except Exception:
            raise CaptureError("portal_unavailable") from None
        self.session = None
        self.closed = False
        self.closed_callback = None
        self.closed_match = None
        self.state_matches = []

    def watch_interruptions(self, callback):
        """Read only logind state; desktops must publish their LockedHint correctly."""
        try:
            bus = self.dbus.SystemBus()
            manager = self.dbus.Interface(
                bus.get_object("org.freedesktop.login1", "/org/freedesktop/login1"),
                "org.freedesktop.login1.Manager",
            )
            path = manager.GetSessionByPID(self.dbus.UInt32(os.getpid()), timeout=5)
            props = self.dbus.Interface(
                bus.get_object("org.freedesktop.login1", path),
                "org.freedesktop.DBus.Properties",
            )
            if props.Get("org.freedesktop.login1.Session", "LockedHint", timeout=5):
                raise CaptureError("screen_locked")

            def changed(interface, values, invalidated):
                if interface == "org.freedesktop.login1.Session" and values.get(
                    "LockedHint"
                ):
                    callback("screen_locked")

            self.state_matches.append(
                bus.add_signal_receiver(
                    changed,
                    signal_name="PropertiesChanged",
                    dbus_interface="org.freedesktop.DBus.Properties",
                    bus_name="org.freedesktop.login1",
                    path=path,
                )
            )
            self.state_matches.append(
                bus.add_signal_receiver(
                    lambda active: callback("system_suspending") if active else None,
                    signal_name="PrepareForSleep",
                    dbus_interface="org.freedesktop.login1.Manager",
                    bus_name="org.freedesktop.login1",
                    path="/org/freedesktop/login1",
                )
            )
            return True
        except CaptureError:
            raise
        except Exception:
            return False

    def _values(self, options):
        types = {str: self.dbus.String, int: self.dbus.UInt32, bool: self.dbus.Boolean}
        return self.dbus.Dictionary(
            {key: types[type(value)](value) for key, value in options.items()},
            signature="sv",
        )

    def request(self, method, *arguments, options=None):
        if method not in {"CreateSession", "SelectSources", "Start"}:
            raise CaptureError("portal_method_invalid")
        # Arguments/results contain tokens and selected-source metadata.
        # Only fixed protocol stages and statuses belong in diagnostics.
        self.diagnostic({"portal_stage": method, "event": "request_started"})
        token = "memulife_" + uuid4().hex
        expected = (
            PATH
            + "/request/"
            + self.bus.get_unique_name()[1:].replace(".", "_")
            + "/"
            + token
        )
        loop, response = self.GLib.MainLoop(), []

        def received(code, result):
            response.append((int(code), dict(result)))
            loop.quit()

        # Subscribe before sending: portal may respond before the method reply.
        match = self.bus.add_signal_receiver(
            received,
            signal_name="Response",
            dbus_interface="org.freedesktop.portal.Request",
            bus_name=SERVICE,
            path=expected,
        )
        timeout = self.GLib.timeout_add_seconds(120, lambda: (loop.quit(), False)[1])
        try:
            handle = str(
                getattr(self.interface, method)(
                    *arguments,
                    self._values((options or {}) | {"handle_token": token}),
                    timeout=30,
                )
            )
            if handle != expected:
                raise CaptureError("portal_request_path_mismatch")
            if not response:
                self.diagnostic({"portal_stage": method, "event": "awaiting_response"})
                loop.run()
            if not response:
                try:
                    self.dbus.Interface(
                        self.bus.get_object(SERVICE, expected),
                        "org.freedesktop.portal.Request",
                    ).Close(timeout=5)
                except Exception:
                    pass
                raise CaptureError("portal_request_timeout")
            code, result = response[0]
            self.diagnostic(
                {
                    "portal_stage": method,
                    "event": "response_received",
                    "response_code": code,
                }
            )
            if code != 0:
                raise CaptureError(
                    "selection_cancelled" if code == 1 else "needs_source_selection"
                )
            return result
        except CaptureError as error:
            error.stage = method
            raise
        except Exception:
            raise CaptureError("portal_request_failed", stage=method) from None
        finally:
            match.remove()
            if self.GLib.MainContext.default().find_source_by_id(timeout):
                self.GLib.source_remove(timeout)

    def create(self):
        result = self.request(
            "CreateSession", options={"session_handle_token": "memulife_" + uuid4().hex}
        )
        handle = result.get("session_handle")
        prefix = (
            PATH + "/session/" + self.bus.get_unique_name()[1:].replace(".", "_") + "/"
        )
        if not isinstance(handle, str) or not handle.startswith(prefix):
            raise CaptureError("portal_session_invalid")
        self.session = self.dbus.ObjectPath(handle)

        def closed(*_):
            self.closed = True
            if self.closed_callback:
                self.closed_callback("portal_session_closed")

        self.closed_match = self.bus.add_signal_receiver(
            closed,
            signal_name="Closed",
            dbus_interface="org.freedesktop.portal.Session",
            bus_name=SERVICE,
            path=handle,
        )

    def select(self, *, source_type, cursor_mode, token):
        options = {"types": source_type, "multiple": False, "cursor_mode": cursor_mode}
        if int(self.properties["version"]) >= 4:
            options["persist_mode"] = 2
            if token:
                options["restore_token"] = token
        self.request("SelectSources", self.session, options=options)

    def start(self):
        return self.request("Start", self.session, "")

    def open_remote(self):
        self.diagnostic(
            {"portal_stage": "OpenPipeWireRemote", "event": "request_started"}
        )
        try:
            if self.closed:
                raise CaptureError("portal_session_closed")
            descriptor = self.interface.OpenPipeWireRemote(
                self.session, self._values({}), timeout=30
            )
            fd = descriptor.take()
            os.set_inheritable(fd, False)
            self.diagnostic(
                {"portal_stage": "OpenPipeWireRemote", "event": "completed"}
            )
            return fd
        except CaptureError as error:
            error.stage = "OpenPipeWireRemote"
            raise
        except Exception:
            raise CaptureError(
                "pipewire_remote_unavailable", stage="OpenPipeWireRemote"
            ) from None

    def close(self):
        for match in self.state_matches:
            match.remove()
        self.state_matches.clear()
        if self.session and not self.closed:
            try:
                self.dbus.Interface(
                    self.bus.get_object(SERVICE, self.session),
                    "org.freedesktop.portal.Session",
                ).Close(timeout=5)
            except Exception:
                pass
        if self.closed_match:
            self.closed_match.remove()
        self.closed = True


def authorize(
    portal,
    store,
    session_id,
    *,
    allow_selection,
    reselect=False,
    source_type=1,
    cursor_mode=2,
):
    if not allow_selection:
        raise CaptureError("needs_source_selection")
    properties = portal.properties
    if int(properties.get("version", 0)) < 2:
        raise CaptureError("portal_version_unsupported")
    if not int(properties.get("AvailableSourceTypes", 0)) & source_type:
        raise CaptureError("source_type_unavailable")
    if not int(properties.get("AvailableCursorModes", 0)) & cursor_mode:
        raise CaptureError("cursor_mode_unavailable")
    token, previous = store.begin(session_id, reselect=reselect)
    try:
        portal.create()
        portal.select(source_type=source_type, cursor_mode=cursor_mode, token=token)
        result = portal.start()
        # Persist the replacement before opening/using the recording stream.
        stream = store.started(session_id, result, previous)
        return stream, portal.open_remote()
    except Exception:
        portal.close()
        raise
