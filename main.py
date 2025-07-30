# Version 1.0.0
try:
    import argparse
    import threading
    import time
    import os
    import json
    from PyATEMMax import ATEMMax

    import tkinter as tk
    from tkinter import messagebox
    from flask import Flask, jsonify, render_template, request
except ImportError as e:
    print(f"Fail import! {e}")

CONFIG_FILE = "config.json"

if not os.path.exists(CONFIG_FILE):
    print(f"{CONFIG_FILE} not found!")
    quit()

with open(CONFIG_FILE, "r") as f:
    config = json.load(f)

# Constants
SWITCHER_IP = config.get("SWITCHER_IP", "172.20.17.17")
DSK_INDEX = 0
PING_INTERVAL = 1.0
CONNECTED = 0

# Shared ATEM Controller class
class ATEMController:
    def __init__(self):
        self.switcher = ATEMMax()
        self.connected = False
        self.on_air = False
        self.tie = False
        self.lock = threading.Lock()
        self.last_shift_time = 0
        self.smart_tie_enabled = False
        self.current_source = None  # Track the program source, init none
        
        # For auto-tie toggle with auto-off timer
        self.auto_tie_enabled = False
        self.auto_tie_timer = None

        # Thread-safe log buffer (list of strings)
        self.log_lines = []
        self.log_lock = threading.Lock()
        
        self._start_sync_thread()

    def _start_sync_thread(self):
        thread = threading.Thread(target=self._sync_loop, daemon=True)
        thread.start()

    def _sync_loop(self):
        while True:
            try:
                if not self.switcher.connected:
                    self.switcher.connect(SWITCHER_IP)
                    time.sleep(1)
                with self.lock:
                    self.connected = self.switcher.connected
                    if self.connected:
                        self.on_air = self.switcher.downstreamKeyer[DSK_INDEX].onAir
                        self.tie = self.switcher.downstreamKeyer[DSK_INDEX].tie
                        self.current_source = self.switcher.programInput[0].videoSource
            except Exception as e:
                self.log(f"Error in sync loop: {e}")
                with self.lock:
                    self.connected = False
                    self.on_air = False
                    self.tie = False
            time.sleep(PING_INTERVAL)

    def log(self, message: str):
        """ Thread-safe log appending """
        with self.log_lock:
            timestamp = time.strftime("%H:%M:%S")
            log_msg = f"[{timestamp}] {message}"
            self.log_lines.append(log_msg)
            # Limit max logs to 1000 lines to avoid memory issues
            if len(self.log_lines) > 1000:
                self.log_lines.pop(0)
        print(log_msg, flush=True)  # Also print to console

    def toggle_auto_key(self):
        with self.lock:
            if self.connected:
                self.switcher.execDownstreamKeyerAutoKeyer(DSK_INDEX)
                time.sleep(0.2)
                self.on_air = self.switcher.downstreamKeyer[DSK_INDEX].onAir
                return True
            return False

    def toggle_tie(self):
        with self.lock:
            if self.connected:
                self.tie = self.switcher.getDownstreamKeyerTie(DSK_INDEX)  # sync state
                self.tie = not self.tie
                time.sleep(0.05)  # fix tie nonresponsive bug?
                self.switcher.setDownstreamKeyerTie(DSK_INDEX, self.tie)
                return True
            return False

    def smart_tie_toggle(self, mE=0):
        with self.lock:
            self.smart_tie_enabled = not self.smart_tie_enabled

            if self.smart_tie_enabled:
                self.switcher.setDownstreamKeyerTie(DSK_INDEX, True)
                self.initial_program_input = self.switcher.programInput[mE].videoSource
                self.log(" [*][SmartTie] Latched. Watching for input change...")
                threading.Thread(target=self._watch_program_change, args=(mE,), daemon=True).start()
            else:
                self.log(" [!][SmartTie] Unlatched.")
                self.switcher.setDownstreamKeyerTie(DSK_INDEX, False)

    def _watch_program_change(self, mE):
        while self.smart_tie_enabled and self.connected:
            current_input = self.switcher.programInput[mE].videoSource
            if current_input != self.initial_program_input:
                self.log(f" [!][SmartTie] Detected program change: {self.initial_program_input} -> {current_input}")
                time.sleep(0.2)
                confirmed_input = self.switcher.programInput[mE].videoSource
                if confirmed_input == current_input:
                    with self.lock:
                        if self.smart_tie_enabled:
                            self.switcher.setDownstreamKeyerTie(DSK_INDEX, False)
                            self.smart_tie_enabled = False
                            self.log(" [*][SmartTie] Tie off after transition.")
                            self.log(" [*][SmartTie] Unlatched.")
                    break
            time.sleep(0.05)

    def toggle_auto_tie(self):
        """Toggle tie on and start 10-second auto off timer, toggle off cancels timer."""
        with self.lock:
            if not self.connected:
                return False

            if self.auto_tie_enabled:
                # Disable tie and cancel timer
                if self.auto_tie_timer:
                    self.auto_tie_timer.cancel()
                    self.auto_tie_timer = None
                self.switcher.setDownstreamKeyerTie(DSK_INDEX, False)
                self.auto_tie_enabled = False
                self.log(" [!][AutoTie] USER Unlatched, Tie off, DSK off.")
                return True

            # Enable tie and start timer
            self.switcher.setDownstreamKeyerTie(DSK_INDEX, True)
            self.auto_tie_enabled = True
            self.log(" [+][AutoTie] Latched, will auto-disable after 10 seconds")

            # Start timer to auto-disable
            self.auto_tie_timer = threading.Timer(10.0, self._auto_tie_off)
            self.auto_tie_timer.start()
            return True

    def _auto_tie_off(self):
        with self.lock:
            if self.auto_tie_enabled and self.connected:
                self.switcher.setDownstreamKeyerTie(DSK_INDEX, False)
                self.auto_tie_enabled = False
                self.auto_tie_timer = None
                self.log(" [*][AutoTie] Automatically disabled after 10 seconds")
    
    def set_dsk_configuration(self):
        with self.lock:
            if not self.connected:
                self.log(" [!][SetDSK] Can't set DSK.")
                self.log(" [!][ConnectionHandler] Are you connected to biobox ethernet?")
                return False
            try:
                self.switcher.setDownstreamKeyerFillSource(DSK_INDEX, 8)
                self.switcher.setDownstreamKeyerKeySource(DSK_INDEX, self.switcher.ATEMVideoSources["MP2 Key"])
                self.switcher.setDownstreamKeyerMasked(DSK_INDEX, True)
                self.switcher.setDownstreamKeyerPreMultiplied(DSK_INDEX, False)
                self.switcher.setDownstreamKeyerClip(DSK_INDEX, 90.5)
                self.switcher.setDownstreamKeyerGain(DSK_INDEX, 100.0)
                self.log(" [+][SetDSK] Downstream keyer configured successfully.")
                return True
            except Exception as e:
                self.log(f" [!!][SetDSK] Error while setting DSK: {e}")
                return False


    def get_status(self):
        with self.lock:
            return {
                "connected": self.connected,
                "on_air": self.on_air,
                "tie": self.tie,
                "smart_tie_enabled": self.smart_tie_enabled,
                "auto_tie_enabled": self.auto_tie_enabled,
                "current_source": self.current_source,
            }

# Tkinter stuffs (not same as Flask)
def run_tk_mode(controller: ATEMController):
    root = tk.Tk()
    root.title("ATEM DSK Controller")

    status_label = tk.Label(root, text="Connecting...", font=('Helvetica', 16), width=25)
    status_label.pack(pady=20)

    tie_label = tk.Label(root, text="TIE: Unknown", font=('Helvetica', 12))
    tie_label.pack(pady=10)

    info_label = tk.Label(root, text="", fg="gray", font=('Helvetica', 10))
    info_label.pack()

    set_dsk_button = tk.Button(root, text="Set DSK", font=('Helvetica', 12), command=lambda: on_set_dsk())
    set_dsk_button.pack(pady=10)


    controller.log(" [!]***********************************************")
    controller.log(" [!]This software sucks.")
    controller.log(" [!]***********************************************")
    time.sleep(1)
    controller.log(f" [*][ConnectionHandler] Searching for ATEM switcher at {SWITCHER_IP}")
    controller.log(" [*][ConnectionHandler] Are you connected to biobox ethernet?")

    def update_ui():
        status = controller.get_status()
        global CONNECTED
        if status["connected"]:
            if CONNECTED == 0:
                controller.log(" [*][ConnectionHandler] ATEM heartbeat received")
                CONNECTED = 1
            status_label.config(
                text="ON AIR" if status["on_air"] else "OFF AIR",
                bg="green" if status["on_air"] else "red",
                fg="white"
            )
            tie_state = "AUTO TIE" if status["auto_tie_enabled"] else ("SMART TIE" if status["smart_tie_enabled"] else ("ON" if status["tie"] else "OFF"))
            tie_label.config(text=f"TIE: {tie_state}")
            info_label.config(text="Connected")
        else:
            CONNECTED = 0
            controller.log(" [!!][ConnectionHandler] ATEM heartbeat lost")
            status_label.config(text="No Connection", bg="gray", fg="white")
            tie_label.config(text="TIE: Unknown")
            info_label.config(text="Can't reach ATEM switcher")
        root.after(500, update_ui)

    def handle_space(event):
        if not controller.toggle_auto_key():
            messagebox.showwarning("Connection Error", "Cannot reach ATEM switcher.")

    def handle_keypress(event):
        if event.keysym in ("Shift_L", "Shift_R"):
            now = time.time()
            if now - controller.last_shift_time > 0.5:
                controller.smart_tie_toggle()
                controller.last_shift_time = now

    def handle_auto_tie(event):
        if not controller.toggle_auto_tie():
            messagebox.showwarning("Connection Error", "Cannot reach ATEM switcher.")

    root.bind("<space>", handle_space)
    root.bind("<KeyPress>", handle_keypress)

    root.bind("t", handle_auto_tie)

    # hope this works
    def on_set_dsk():
        if not controller.set_dsk_configuration():
            messagebox.showerror("Error", "Failed to set DSK. Not connected?")

    update_ui()
    root.mainloop()

# Flask Web UI
def run_flask_mode(controller: ATEMController):
    app = Flask(__name__)

    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/status")
    def status():
        return jsonify(controller.get_status())

    @app.route("/toggle_auto_key", methods=["POST"])
    def toggle_auto_key():
        if controller.toggle_auto_key():
            return jsonify(success=True)
        return jsonify(success=False), 500

    @app.route("/toggle_tie", methods=["POST"])
    def toggle_tie():
        if controller.toggle_tie():
            return jsonify(success=True)
        return jsonify(success=False), 500

    @app.route("/smart_tie_toggle", methods=["POST"])
    def smart_tie_toggle():
        controller.smart_tie_toggle()
        return jsonify(success=True)

    @app.route("/toggle_auto_tie", methods=["POST"])
    def toggle_auto_tie():
        if controller.toggle_auto_tie():
            return jsonify(success=True)
        return jsonify(success=False), 500
    
    @app.route("/set_dsk", methods=["POST"])
    def set_dsk():
        if controller.set_dsk_configuration():
            return jsonify(success=True)
        return jsonify(success=False), 500


    @app.route("/logs")
    def logs():
        with controller.log_lock:
            return jsonify(controller.log_lines)
        

    app.run(
    debug=True, host=config.get("FLASK_HOST", "127.0.0.1"), port=5000)


# Main
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--tk", action="store_true", help="Run in Tkinter desktop mode")
    parser.add_argument("--flask", action="store_true", help="Run in Flask web server mode")
    args = parser.parse_args()

    controller = ATEMController()


    with open(__file__, "r") as f:
        first_line = f.readline()
        print(first_line.strip())


    if args.tk:
        controller.log(" [*][main] --tk parse detected! starting desktopui")
        run_tk_mode(controller)
    elif args.flask:
        controller.log(" [*][main] --flask parse detected! starting webui")
        run_flask_mode(controller)
    else:
        controller.log(" [!][main] Specify either --tk or --flask")
