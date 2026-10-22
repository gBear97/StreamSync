// sstap - StreamSync's recorder for "this Mac's sound" (macOS 14.2+).
//
// Records everything the Mac plays, except the processes it is told to
// leave out (StreamSync itself, so the film's own sound never counts),
// through a Core Audio process tap, and writes it to stdout (or --out) as
// a stream of records for audio_capture.py:
//
//   "SSJ1" u32 length, JSON          - hello / status / event / error
//   "SSA1" u32 source, f64 host_time, f64 sample_time, f64 now_time,
//          u32 frames, u32 flags, f32 x frames   - mono audio
//
// host_time is when CoreAudio says the block's first sample was captured,
// in seconds of mach_absolute_time - the clock Python's time.perf_counter()
// reads on macOS - so the app gets capture times, not arrival times.
// now_time is when CoreAudio handed the block over (for diagnostics).
//
// --compare NAME also records an input device whose name contains NAME
// (BlackHole, a microphone) as another source, on the same clock: that is
// how the tap's timing is checked against the alternatives.

import CoreAudio
import Foundation

// MARK: - options

var excludePIDs: [pid_t] = []
var compareNames: [String] = []
var outPath = "-"
var seconds: Double? = nil

do {
    var args = Array(CommandLine.arguments.dropFirst())
    func value() -> String {
        guard !args.isEmpty else {
            FileHandle.standardError.write("missing value\n".data(using: .utf8)!)
            exit(64)
        }
        return args.removeFirst()
    }
    while !args.isEmpty {
        switch args.removeFirst() {
        case "--exclude-pid": excludePIDs.append(pid_t(value()) ?? 0)
        case "--exclude-parent": excludePIDs.append(getppid())
        case "--compare": compareNames.append(value())
        case "--out": outPath = value()
        case "--seconds": seconds = Double(value())
        case let other:
            FileHandle.standardError.write("unknown option \(other)\n".data(using: .utf8)!)
            exit(64)
        }
    }
}

// MARK: - output

let out: UnsafeMutablePointer<FILE> = {
    if outPath == "-" { return stdout }
    guard let f = fopen(outPath, "wb") else { exit(73) }
    return f
}()
setvbuf(out, nil, _IOFBF, 1 << 16)
let outLock = NSLock()

func emit(_ obj: [String: Any]) {
    guard let data = try? JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys]) else { return }
    outLock.lock(); defer { outLock.unlock() }
    fputs("SSJ1", out)
    var len = UInt32(data.count).littleEndian
    fwrite(&len, 4, 1, out)
    data.withUnsafeBytes { _ = fwrite($0.baseAddress, 1, data.count, out) }
    fflush(out)
}

func emitAudio(source: UInt32, hostTime: Double, sampleTime: Double, nowTime: Double,
               flags: UInt32, samples: UnsafeBufferPointer<Float>) {
    outLock.lock(); defer { outLock.unlock() }
    fputs("SSA1", out)
    var s = source.littleEndian, h = hostTime, st = sampleTime, nt = nowTime
    var n = UInt32(samples.count).littleEndian, fl = flags.littleEndian
    fwrite(&s, 4, 1, out); fwrite(&h, 8, 1, out); fwrite(&st, 8, 1, out)
    fwrite(&nt, 8, 1, out); fwrite(&n, 4, 1, out); fwrite(&fl, 4, 1, out)
    _ = fwrite(samples.baseAddress, 4, samples.count, out)
    fflush(out)
}

func fail(_ code: String, _ message: String, status: Int32 = 2) -> Never {
    emit(["type": "error", "code": code, "message": message])
    exit(status)
}

// MARK: - CoreAudio helpers

let system = AudioObjectID(kAudioObjectSystemObject)

func address(_ sel: AudioObjectPropertySelector,
             _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> AudioObjectPropertyAddress {
    AudioObjectPropertyAddress(mSelector: sel, mScope: scope, mElement: kAudioObjectPropertyElementMain)
}

func getU32(_ obj: AudioObjectID, _ sel: AudioObjectPropertySelector,
            _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> UInt32? {
    var a = address(sel, scope); var v: UInt32 = 0; var size = UInt32(4)
    return AudioObjectGetPropertyData(obj, &a, 0, nil, &size, &v) == noErr ? v : nil
}

func getF64(_ obj: AudioObjectID, _ sel: AudioObjectPropertySelector) -> Double? {
    var a = address(sel); var v = 0.0; var size = UInt32(8)
    return AudioObjectGetPropertyData(obj, &a, 0, nil, &size, &v) == noErr ? v : nil
}

func getString(_ obj: AudioObjectID, _ sel: AudioObjectPropertySelector) -> String? {
    var a = address(sel); var cf: Unmanaged<CFString>? = nil
    var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    guard AudioObjectGetPropertyData(obj, &a, 0, nil, &size, &cf) == noErr, let s = cf else { return nil }
    return s.takeRetainedValue() as String
}

func getIDs(_ obj: AudioObjectID, _ sel: AudioObjectPropertySelector,
            _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> [AudioObjectID] {
    var a = address(sel, scope); var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(obj, &a, 0, nil, &size) == noErr, size > 0 else { return [] }
    var ids = [AudioObjectID](repeating: 0, count: Int(size) / 4)
    return AudioObjectGetPropertyData(obj, &a, 0, nil, &size, &ids) == noErr ? ids : []
}

func deviceForUID(_ uid: String) -> AudioObjectID? {
    var a = address(kAudioHardwarePropertyTranslateUIDToDevice)
    var cfUID = uid as CFString
    var dev = AudioObjectID(kAudioObjectUnknown)
    var size = UInt32(MemoryLayout<AudioObjectID>.size)
    let err = withUnsafeMutablePointer(to: &cfUID) {
        AudioObjectGetPropertyData(system, &a, UInt32(MemoryLayout<CFString>.size), $0, &size, &dev)
    }
    return err == noErr && dev != kAudioObjectUnknown ? dev : nil
}

func processObject(pid: pid_t) -> AudioObjectID? {
    var a = address(kAudioHardwarePropertyTranslatePIDToProcessObject)
    var p = pid
    var obj = AudioObjectID(kAudioObjectUnknown)
    var size = UInt32(MemoryLayout<AudioObjectID>.size)
    let err = AudioObjectGetPropertyData(system, &a, UInt32(MemoryLayout<pid_t>.size), &p, &size, &obj)
    return err == noErr && obj != kAudioObjectUnknown ? obj : nil
}

func latency(_ dev: AudioObjectID, _ scope: AudioObjectPropertyScope) -> [String: Any] {
    let streams = getIDs(dev, kAudioDevicePropertyStreams, scope)
    return [
        "device": getU32(dev, kAudioDevicePropertyLatency, scope) ?? 0,
        "safety": getU32(dev, kAudioDevicePropertySafetyOffset, scope) ?? 0,
        "stream": streams.first.flatMap { getU32($0, kAudioStreamPropertyLatency) } ?? 0,
        "buffer": getU32(dev, kAudioDevicePropertyBufferFrameSize) ?? 0,
        "rate": getF64(dev, kAudioDevicePropertyNominalSampleRate) ?? 0,
    ]
}

func describe(_ dev: AudioObjectID) -> [String: Any] {
    ["id": dev, "name": getString(dev, kAudioObjectPropertyName) ?? "?",
     "uid": getString(dev, kAudioDevicePropertyDeviceUID) ?? "?"]
}

/// The default output, and the device whose delay the listener hears:
/// itself, or for an aggregate (a Multi-Output Device) the sub-device
/// that clocks it.
func outputDevices() -> (AudioObjectID, AudioObjectID) {
    let dflt = getIDs(system, kAudioHardwarePropertyDefaultOutputDevice).first ?? 0
    if getU32(dflt, kAudioDevicePropertyTransportType) == kAudioDeviceTransportTypeAggregate,
       let uid = getString(dflt, kAudioAggregateDevicePropertyMainSubDevice),
       let sub = deviceForUID(uid) {
        return (dflt, sub)
    }
    return (dflt, dflt)
}

var timebase = mach_timebase_info_data_t()
mach_timebase_info(&timebase)
func hostSeconds(_ t: UInt64) -> Double {
    Double(t) * Double(timebase.numer) / Double(timebase.denom) / 1e9
}

// MARK: - a recording source

final class Source {
    let id: UInt32
    let device: AudioObjectID
    var proc: AudioDeviceIOProcID?
    var mono = [Float](repeating: 0, count: 16384)

    init(id: UInt32, device: AudioObjectID) {
        self.id = id; self.device = device
    }

    func start(on queue: DispatchQueue) -> OSStatus {
        var err = AudioDeviceCreateIOProcIDWithBlock(&proc, device, queue) {
            [unowned self] now, input, inputTime, _, _ in
            self.deliver(input, inputTime.pointee, now.pointee)
        }
        if err == noErr { err = AudioDeviceStart(device, proc) }
        return err
    }

    func stop() {
        if let p = proc {
            AudioDeviceStop(device, p)
            AudioDeviceDestroyIOProcID(device, p)
            proc = nil
        }
    }

    func deliver(_ input: UnsafePointer<AudioBufferList>, _ ts: AudioTimeStamp, _ now: AudioTimeStamp) {
        let list = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: input))
        var frames = -1, channels = 0
        for buf in list {
            let ch = Int(buf.mNumberChannels)
            guard ch > 0, let raw = buf.mData else { continue }
            let n = Int(buf.mDataByteSize) / (4 * ch)
            if frames < 0 {
                frames = min(n, mono.count)
                for f in 0..<frames { mono[f] = 0 }
            }
            let p = raw.assumingMemoryBound(to: Float.self)
            for f in 0..<min(frames, n) {
                var s: Float = 0
                for c in 0..<ch { s += p[f * ch + c] }
                mono[f] += s
            }
            channels += ch
        }
        guard frames > 0, channels > 0 else { return }
        let scale = 1 / Float(channels)
        for f in 0..<frames { mono[f] *= scale }
        var flags: UInt32 = 0
        if ts.mFlags.contains(.hostTimeValid) { flags |= 1 }
        if ts.mFlags.contains(.sampleTimeValid) { flags |= 2 }
        mono.withUnsafeBufferPointer {
            emitAudio(source: id, hostTime: hostSeconds(ts.mHostTime), sampleTime: ts.mSampleTime,
                      nowTime: hostSeconds(now.mHostTime), flags: flags,
                      samples: UnsafeBufferPointer(rebasing: $0[0..<frames]))
        }
    }
}

// MARK: - main

let ioQueue = DispatchQueue(label: "sstap.io", qos: .userInteractive)
var sources: [Source] = []
var keepAlive: [AnyObject] = []     // timers and signal sources run() starts
var tapID = AudioObjectID(kAudioObjectUnknown)
var aggregateID = AudioObjectID(kAudioObjectUnknown)

func cleanup() {
    for s in sources { s.stop() }
    if aggregateID != kAudioObjectUnknown { AudioHardwareDestroyAggregateDevice(aggregateID) }
    if #available(macOS 14.2, *), tapID != kAudioObjectUnknown { AudioHardwareDestroyProcessTap(tapID) }
    outLock.lock(); fflush(out); outLock.unlock()
}

@available(macOS 14.2, *)
func run() {
    // The processes to leave out. A process only has an audio object once it
    // has used audio, so ones that are missing now are added when they appear.
    var excludedObjects: [AudioObjectID] = []
    func refreshExclusions() -> Bool {
        let found = excludePIDs.compactMap { processObject(pid: $0) }
        if Set(found) == Set(excludedObjects) { return false }
        excludedObjects = found
        return true
    }
    _ = refreshExclusions()

    let description = CATapDescription(stereoGlobalTapButExcludeProcesses: excludedObjects)
    description.uuid = UUID()
    description.name = "StreamSync"
    description.isPrivate = true
    description.muteBehavior = .unmuted
    var err = AudioHardwareCreateProcessTap(description, &tapID)
    if err != noErr {
        fail("tap-failed", "macOS would not let StreamSync tap this Mac's sound (error \(err)). "
             + "Allow it in System Settings > Privacy & Security > Screen & System Audio "
             + "Recording > System Audio Recording Only.")
    }

    // The tap alone, no output device beside it: an aggregate that held the
    // output would run that device's input too, switching AirPods to their
    // call profile or lighting a headset's microphone. Tested on its own,
    // the tap kept time with BlackHole to the microsecond over a minute.
    let (defaultOutput, heard) = outputDevices()
    let aggregate: [String: Any] = [
        kAudioAggregateDeviceNameKey: "StreamSync tap",
        kAudioAggregateDeviceUIDKey: "streamsync.tap." + UUID().uuidString,
        kAudioAggregateDeviceIsPrivateKey: true,
        kAudioAggregateDeviceIsStackedKey: false,
        kAudioAggregateDeviceTapAutoStartKey: true,
        kAudioAggregateDeviceTapListKey: [[
            kAudioSubTapUIDKey: description.uuid.uuidString,
            kAudioSubTapDriftCompensationKey: true,
        ]],
    ]
    err = AudioHardwareCreateAggregateDevice(aggregate as CFDictionary, &aggregateID)
    if err != noErr {
        cleanup()
        fail("aggregate-failed", "Could not create the tap's device (error \(err)).")
    }

    var tapFormat = AudioStreamBasicDescription()
    var fmtSize = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
    var fmtAddr = address(kAudioTapPropertyFormat)
    AudioObjectGetPropertyData(tapID, &fmtAddr, 0, nil, &fmtSize, &tapFormat)

    sources.append(Source(id: 0, device: aggregateID))
    var compared: [[String: Any]] = []
    for (i, name) in compareNames.enumerated() {
        let match = getIDs(system, kAudioHardwarePropertyDevices).first {
            !getIDs($0, kAudioDevicePropertyStreams, kAudioObjectPropertyScopeInput).isEmpty &&
            (getString($0, kAudioObjectPropertyName) ?? "").localizedCaseInsensitiveContains(name)
        }
        guard let dev = match else { cleanup(); fail("no-device", "No input device named like \(name).") }
        sources.append(Source(id: UInt32(i + 1), device: dev))
        var d = describe(dev)
        d["source"] = i + 1
        d["latency"] = latency(dev, kAudioObjectPropertyScopeInput)
        compared.append(d)
    }

    var outputInfo = describe(defaultOutput)
    outputInfo["latency"] = latency(heard, kAudioObjectPropertyScopeOutput)
    outputInfo["heard"] = describe(heard)
    emit([
        "type": "hello", "version": 1, "pid": getpid(),
        "tap": ["rate": tapFormat.mSampleRate, "channels": tapFormat.mChannelsPerFrame,
                "aggregate_rate": getF64(aggregateID, kAudioDevicePropertyNominalSampleRate) ?? 0,
                "excluded_pids": excludePIDs.map { Int($0) }, "excluded_now": excludedObjects.count],
        "output": outputInfo,
        "compare": compared,
    ])

    for s in sources {
        let e = s.start(on: ioQueue)
        if e != noErr { cleanup(); fail("start-failed", "Could not start recording (error \(e)).") }
    }

    // Once a second: is anything other than the excluded processes playing,
    // and has a process to exclude appeared? "playing" lets the app tell
    // "nothing is playing" from "macOS is not letting StreamSync hear it".
    var lastPlaying: Bool? = nil
    let ticker = DispatchSource.makeTimerSource(queue: .main)
    ticker.schedule(deadline: .now() + 1, repeating: 1)
    ticker.setEventHandler {
        if refreshExclusions() {
            description.processes = excludedObjects
            // The property's data is the description object itself, so
            // what goes in is a pointer to that object reference.
            var a = address(kAudioTapPropertyDescription)
            var ref: UnsafeMutableRawPointer? = Unmanaged.passUnretained(description).toOpaque()
            AudioObjectSetPropertyData(tapID, &a, 0, nil,
                                       UInt32(MemoryLayout<UnsafeMutableRawPointer?>.size), &ref)
            emit(["type": "status", "excluded_now": excludedObjects.count])
        }
        let mine = Set(excludedObjects)
        let me = processObject(pid: getpid())
        let playing = getIDs(system, kAudioHardwarePropertyProcessObjectList).contains {
            !mine.contains($0) && $0 != me && getU32($0, kAudioProcessPropertyIsRunningOutput) == 1
        }
        if playing != lastPlaying {
            lastPlaying = playing
            emit(["type": "status", "playing": playing])
        }
    }
    ticker.resume()
    keepAlive.append(ticker)

    // A new default output changes the delay the app adds: say so and
    // stop, and the app starts a fresh recorder.
    var outAddr = address(kAudioHardwarePropertyDefaultOutputDevice)
    AudioObjectAddPropertyListenerBlock(system, &outAddr, .main) { _, _ in
        emit(["type": "event", "event": "output-changed"])
        cleanup()
        exit(3)
    }

    for sig in [SIGTERM, SIGINT] {
        signal(sig, SIG_IGN)
        let src = DispatchSource.makeSignalSource(signal: sig, queue: .main)
        src.setEventHandler { cleanup(); exit(0) }
        src.resume()
        keepAlive.append(src)
    }

    if outPath == "-" {
        // The app closes our stdin to stop us (and it closes when the app dies).
        Thread.detachNewThread {
            while FileHandle.standardInput.availableData.count > 0 {}
            DispatchQueue.main.async { cleanup(); exit(0) }
        }
    }
    if let s = seconds {
        DispatchQueue.main.asyncAfter(deadline: .now() + s) { cleanup(); exit(0) }
    }
}

if #available(macOS 14.2, *) {
    run()
} else {
    fail("needs-14.2", "Recording this Mac's sound needs macOS 14.2 or later.")
}
dispatchMain()
