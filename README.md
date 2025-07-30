# ATEM-Controller
Custom ATEM software built for my school. Built on the PyATEMMax library.

The main use is to execute DSK through a webserver Flask to control our ATEM switcher with an iPad. The ATEM Mini Extreme doesn't have a hardware button to auto DSK (fade in) and tie so I made this program.

Initially we used the offical ATEM app but it wasn't intuitive and messy for our switcher. Then, we used the Tkinter to auto fade and tie.

I've adapted the Tkinter to a Flask webserver. The Tkinter still exists but is a bit out of date with the Flask.

To use the software you must have a config file `config.json` in place. It follows this format:

```
{
  "SWITCHER_IP": "xxx.xx.xx.xx",
  "FLASK_HOST": "xxx.xx.xx.xx"
}
```