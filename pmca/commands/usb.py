from __future__ import print_function
import io
import json
import os
import sys
import time

if sys.version_info < (3,):
 # Python 2
 input = raw_input

import config
from .. import appstore
from .. import firmware
from .. import installer
from ..marketserver.server import *
from ..usb import *
from ..usb import usbshell
from ..usb.driver import *
from ..usb.sony import *
from ..util import http

scriptRoot = getattr(sys, '_MEIPASS', os.path.dirname(__file__) + '/../..')


def printStatus(status):
 """Print progress"""
 print('%s %d%%' % (status.message, status.percent))


def switchToAppInstaller(dev):
 """Switches a camera in MTP mode to app installation mode"""
 print('Switching to app install mode')
 SonyExtCmdCamera(dev).switchToAppInstaller()


appListCache = None
def listApps(enableCache=False):
 global appListCache
 remoteAppStore = RemoteAppStore(config.appengineServer)
 appStoreRepo = appstore.GithubApi(config.githubAppListUser, config.githubAppListRepo)

 if not appListCache or not enableCache:
  print('Loading app list')
  try:
   apps = remoteAppStore.listApps()
  except:
   print('Cannot connect to remote server, falling back to appstore repository')
   apps = appstore.AppStore(appStoreRepo).apps
  print('Found %d apps' % len(apps))
  appListCache = apps
 return appListCache


def installApp(dev, apkFile=None, appPackage=None, outFile=None, local=False):
 """Installs an app on the specified device."""
 certFile = scriptRoot + '/certs/localtest.me.pem'
 with ServerContext(LocalMarketServer(certFile, config.officialServer)) as server:
  if apkFile:
   server.setApk(apkFile.read())
  elif appPackage:
   print('Downloading apk')
   apps = listApps(True)
   if appPackage not in apps:
    raise Exception('Unknown app: %s' % appPackage)
   server.setApk(apps[appPackage].release.asset)

  print('Starting task')
  xpdData = server.getXpd()

  print('Starting communication')
  # Point the camera to the web api
  result = installer.install(dev, server.host, server.port, xpdData, printStatus)
  if result.code != 0:
   raise Exception('Communication error %d: %s' % (result.code, result.message))

  result = server.getResult()

  if not local:
   try:
    RemoteAppStore(config.appengineServer).sendStats(result)
   except:
    pass

  print('Task completed successfully')

  if outFile:
   print('Writing to output file')
   json.dump(result, outFile, indent=2)

  return result


class UsbDriverList:
 def __init__(self, *contexts):
  self._contexts = contexts
  self._drivers = []

 def __enter__(self):
  self._drivers = [context.__enter__() for context in self._contexts]
  return self

 def __exit__(self, *ex):
  for context in self._contexts:
   context.__exit__(*ex)
  self._drivers = []

 def listDevices(self, vendor):
  for driver in self._drivers:
   for dev in driver.listDevices(vendor):
    yield dev, driver.classType, driver.openDevice(dev)


def importDriver(driverName=None):
 """Imports the usb driver. Use in a with statement"""
 MscContext = None
 MtpContext = None

 # Load native drivers
 if driverName == 'native' or driverName is None:
  if sys.platform == 'win32':
   from ..usb.driver.windows.msc import MscContext
   from ..usb.driver.windows.wpd import MtpContext
  elif sys.platform == 'darwin':
   from ..usb.driver.osx import MscContext
  else:
   print('No native drivers available')
 elif driverName != 'libusb':
  raise Exception('Unknown driver')

 # Fallback to libusb
 if MscContext is None:
  from ..usb.driver.libusb import MscContext
 if MtpContext is None:
  from ..usb.driver.libusb import MtpContext

 drivers = [MscContext(), MtpContext()]
 print('Using drivers %s' % ', '.join(d.name for d in drivers))
 return UsbDriverList(*drivers)


def listDevices(driverList, quiet=False):
 """List all Sony usb devices"""
 if not quiet:
  print('Looking for Sony devices')
 for dev, type, drv in driverList.listDevices(SONY_ID_VENDOR):
  if type == USB_CLASS_MSC:
   if not quiet:
    print('\nQuerying mass storage device')
   # Get device info
   info = MscDevice(drv).getDeviceInfo()

   if isSonyMscCamera(info):
    if isSonyUpdaterCamera(dev):
     if not quiet:
      print('%s %s is a camera in updater mode' % (info.manufacturer, info.model))
     yield SonyMscUpdaterCamera(drv)
    else:
     if not quiet:
      print('%s %s is a camera in mass storage mode' % (info.manufacturer, info.model))
     yield SonyMscCamera(drv)

  elif type == USB_CLASS_PTP:
   if not quiet:
    print('\nQuerying MTP device')
   # Get device info
   info = MtpDevice(drv).getDeviceInfo()

   if isSonyMtpCamera(info):
    if not quiet:
     print('%s %s is a camera in MTP mode' % (info.manufacturer, info.model))
    yield SonyMtpCamera(drv)
   elif isSonyMtpAppInstaller(info):
    if not quiet:
     print('%s %s is a camera in app install mode' % (info.manufacturer, info.model))
    yield SonyMtpAppInstaller(drv)
  if not quiet:
   print('')


def getDevice(driver):
 """Check for exactly one Sony usb device"""
 devices = list(listDevices(driver))
 if not devices:
  print('No devices found. Ensure your camera is connected.')
 elif len(devices) != 1:
  print('Too many devices found. Only one camera is supported')
 else:
  return devices[0]


def infoCommand(driverName=None):
 """Display information about the camera connected via usb"""
 with importDriver(driverName) as driver:
  device = getDevice(driver)
  if device:
   if isinstance(device, SonyMtpAppInstaller):
    info = installApp(device)
    print('')
    props = [
     ('Model', info['deviceinfo']['name']),
     ('Product code', info['deviceinfo']['productcode']),
     ('Serial number', info['deviceinfo']['deviceid']),
     ('Firmware version', info['deviceinfo']['fwversion']),
    ]
   else:
    dev = SonyExtCmdCamera(device)
    info = dev.getCameraInfo()
    updater = SonyUpdaterCamera(device)
    updater.init()
    firmwareOld, firmwareNew = updater.getFirmwareVersion()
    props = [
     ('Model', info.modelName),
     ('Product code', info.modelCode),
     ('Serial number', info.serial),
     ('Firmware version', firmwareOld),
    ]
    try:
     lensInfo = dev.getLensInfo()
     if lensInfo.model != 0:
      props.append(('Lens', 'Model 0x%x (Firmware %s)' % (lensInfo.model, lensInfo.version)))
    except (InvalidCommandException, UnknownMscException):
     pass
    try:
     gpsInfo = dev.getGpsData()
     props.append(('GPS Data', '%s - %s' % gpsInfo))
    except (InvalidCommandException, UnknownMscException):
     pass
   for k, v in props:
    print('%-20s%s' % (k + ': ', v))


def installCommand(driverName=None, apkFile=None, appPackage=None, outFile=None, local=False):
 """Install the given apk on the camera"""
 with importDriver(driverName) as driver:
  device = getDevice(driver)
  if device and not isinstance(device, SonyMtpAppInstaller):
   switchToAppInstaller(device)
   device = None

   print('Waiting for camera to switch...')
   for i in range(10):
    time.sleep(.5)
    try:
     devices = list(listDevices(driver, True))
     if len(devices) == 1 and isinstance(devices[0], SonyMtpAppInstaller):
      device = devices[0]
      break
    except:
     pass
   else:
    print('Operation timed out. Please run this command again when your camera has connected.')

  if device:
   installApp(device, apkFile, appPackage, outFile, local)


def appSelectionCommand():
 apps = list(listApps().values())
 for i, app in enumerate(apps):
  print(' [%2d] %s' % (i+1, app.package))
 i = int(input('Enter number of app to install (0 to abort): '))
 if i != 0:
  pkg = apps[i - 1].package
  print('')
  print('Installing %s' % pkg)
  return pkg


def getFdats():
 fdatDir = scriptRoot + '/updatershell/fdat/'
 for dir in os.listdir(fdatDir):
  if os.path.isdir(fdatDir + dir):
   payloadFile = fdatDir + dir + '.dat'
   if os.path.isfile(payloadFile):
    for model in os.listdir(fdatDir + dir):
     hdrFile = fdatDir + dir + '/' + model
     if os.path.isfile(hdrFile) and hdrFile.endswith('.hdr'):
      yield model[:-4], (hdrFile, payloadFile)


def getFdat(device):
 fdats = dict(getFdats())
 if device.endswith('V') and device not in fdats:
  device = device[:-1]
 if device in fdats:
  hdrFile, payloadFile = fdats[device]
  with open(hdrFile, 'rb') as hdr, open(payloadFile, 'rb') as payload:
   return hdr.read() + payload.read()


def firmwareUpdateCommand(file, driverName=None):
 offset, size = firmware.readDat(file)

 with importDriver(driverName) as driver:
  device = getDevice(driver)
  if device:
   firmwareUpdateCommandInternal(driver, device, file, offset, size)


def updaterShellCommand(model=None, fdatFile=None, driverName=None, complete=None):
 with importDriver(driverName) as driver:
  device = getDevice(driver)
  if device:
   if fdatFile:
    fdat = fdatFile.read()
   else:
    if not model:
     print('Getting device info')
     model = SonyExtCmdCamera(device).getCameraInfo().modelName
     print('Using firmware for model %s' % model)
     print('')

    fdat = getFdat(model)
    if not fdat:
     print('Unknown device: %s' % model)
     return

   if not complete:
    def complete(device):
     print('Starting updater shell...')
     print('')
     usbshell.usbshell_loop(device)
   firmwareUpdateCommandInternal(driver, device, io.BytesIO(fdat), 0, len(fdat), complete)


def firmwareUpdateCommandInternal(driver, device, file, offset, size, complete=None):
 if isinstance(device, SonyMtpAppInstaller):
  print('Error: Cannot use camera in app install mode. Please restart the device.')
  return

 dev = SonyUpdaterCamera(device)

 print('Initializing firmware update')
 dev.init()
 file.seek(offset)
 dev.checkGuard(file, size)
 versions = dev.getFirmwareVersion()
 if versions[1] != '9.99':
  print('Updating from version %s to version %s' % versions)

 if not isinstance(device, SonyMscUpdaterCamera):
  print('Switching to updater mode')
  dev.switchMode()

  device = None
  print('')
  print('Waiting for camera to switch...')
  print('Please follow the instructions on the camera screen.')
  for i in range(60):
   time.sleep(.5)
   try:
    devices = list(listDevices(driver, True))
    if len(devices) == 1 and isinstance(devices[0], SonyMscUpdaterCamera):
     device = devices[0]
     break
   except:
    pass
  else:
   print('Operation timed out. Please run this command again when your camera has connected.')

  if device:
   firmwareUpdateCommandInternal(None, device, file, offset, size, complete)

 else:
  def progress(written, total):
   p = int(written * 20 / total) * 5
   if p != progress.percent:
    print('%d%%' % p)
    progress.percent = p
  progress.percent = -1

  print('Writing firmware')
  file.seek(offset)
  dev.writeFirmware(file, size, progress, complete)
  dev.complete()
  print('Done')


def gpsUpdateCommand(file=None, driverName=None):
 with importDriver(driverName) as driver:
  device = getDevice(driver)
  if device:
   if isinstance(device, SonyMtpAppInstaller):
    print('Error: Cannot use camera in app install mode. Please restart the device.')
    return

   if not file:
    print('Downloading GPS data')
    file = io.BytesIO(http.get('https://control.d-imaging.sony.co.jp/GPS/assistme.dat').raw_data)

   print('Writing GPS data')
   SonyExtCmdCamera(device).writeGpsData(file)
   print('Done')


def streamingCommand(write=None, file=None, driverName=None):
 """Read/Write Streaming information for the camera connected via usb"""
 with importDriver(driverName) as driver:
  device = getDevice(driver)
  if device:
   if isinstance(device, SonyMtpAppInstaller):
    info = installApp(device)
    print('')
    props = [
     ('Model', info['deviceinfo']['name']),
     ('Product code', info['deviceinfo']['productcode']),
     ('Serial number', info['deviceinfo']['deviceid']),
     ('Firmware version', info['deviceinfo']['fwversion']),
    ]
   else:
    dev = SonyExtCmdCamera(device)
    # Read settings from camera (do this first so we know channels/supportedFormats)
    (info1, info2, info3, channels, supportedFormats, qty) = dev.getLiveStreamingServiceInfo()
    social = dev.getLiveStreamingSocialInfo()

    if write:
     if qty != 1:
      print("QTY is more than 1, panic!")
      return

     # Write camera settings from file
     props = json.load(write)
     mydict = {}
     for item in props:
      mydict[item[0]]=item[1]

     newinfo1 = SonyExtCmdCamera.LiveStreamingServiceInfo1.pack(
      service = mydict['service'],
      enabled = mydict['enabled'],
      macId = mydict['macId'].encode(),
      macSecret = mydict['macSecret'].encode(),
      macIssueTime = binascii.a2b_hex(mydict['macIssueTime']),
      unknown = 0,
     )

     newinfo2 = SonyExtCmdCamera.LiveStreamingServiceInfo2.pack(
      shortURL = mydict['shortURL'].encode(),
      videoFormat = mydict['videoFormat'],
     )

     newinfo3 = SonyExtCmdCamera.LiveStreamingServiceInfo3.pack(
      enableRecordMode = mydict['enableRecordMode'],
      videoTitle = mydict['videoTitle'].encode(),
      videoDescription = mydict['videoDescription'].encode(),
      videoTag = mydict['videoTag'].encode(),
     )

     # nasty re-assemble
     data = (1).to_bytes(4, byteorder='little')
     data += (qty).to_bytes(4, byteorder='little')
     data += newinfo1
     data += len(channels).to_bytes(4, byteorder='little')
     for j in range(len(channels)):
      data += channels[j].to_bytes(4, byteorder='little')
     data += newinfo2
     data += len(supportedFormats).to_bytes(4, byteorder='little')
     for j in range(len(supportedFormats)):
      data += supportedFormats[j].to_bytes(4, byteorder='little')
     data += newinfo3

     dev.setLiveStreamingServiceInfo(data)

     newsocial = SonyExtCmdCamera.LiveStreamingSNSInfo.pack(
      twitterEnabled = mydict['twitterEnabled'],
      twitterConsumerKey = mydict['twitterConsumerKey'].encode(),
      twitterConsumerSecret = mydict['twitterConsumerSecret'].encode(),
      twitterAccessToken1 = mydict['twitterAccessToken1'].encode(),
      twitterAccessTokenSecret = mydict['twitterAccessTokenSecret'].encode(),
      twitterMessage = mydict['twitterMessage'].encode(),
      facebookEnabled = mydict['facebookEnabled'],
      facebookAccessToken = mydict['facebookAccessToken'].encode(),
      facebookMessage = mydict['facebookMessage'].encode(),
     )
     dev.setLiveStreamingSocialInfo(newsocial)
     return

    props = [
     ('service', info1.service),
     ('enabled', info1.enabled),
     ('macId', info1.macId.decode('ascii')),
     ('macSecret', info1.macSecret.decode('ascii')),
     ('macIssueTime', binascii.b2a_hex(info1.macIssueTime).decode('ascii')),
     ('shortURL', info2.shortURL.decode('ascii')),
     ('videoFormat', info2.videoFormat),
     ('enableRecordMode', info3.enableRecordMode),
     ('videoTitle', info3.videoTitle.decode('ascii')),
     ('videoDescription', info3.videoDescription.decode('ascii')),
     ('videoTag', info3.videoTag.decode('ascii')),
     ('twitterEnabled', social.twitterEnabled),
     ('twitterConsumerKey', social.twitterConsumerKey.decode('ascii')),
     ('twitterConsumerSecret', social.twitterConsumerSecret.decode('ascii')),
     ('twitterAccessToken1', social.twitterAccessToken1.decode('ascii')),
     ('twitterAccessTokenSecret', social.twitterAccessTokenSecret.decode('ascii')),
     ('twitterMessage', social.twitterMessage.decode('ascii')),
     ('facebookEnabled', social.facebookEnabled),
     ('facebookAccessToken', social.facebookAccessToken.decode('ascii')),
     ('facebookMessage', social.facebookMessage.decode('ascii')),
    ]

   if file:
    file.write(json.dumps(props))
   else:
    # Just print to screen
    for k, v in props:
     print('%-20s%s' % (k + ': ', v))
