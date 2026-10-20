// au_probe — instantiate an Audio Unit, optionally load a state plist back into
// it through kAudioUnitProperty_ClassInfo, then dump every parameter's value.
//
// This is the tool behind "perturbation decoding": feed a hand-edited state blob
// to the plug-in and see which parameter moves. The plug-in is the only authority
// on its own serialisation, so asking it beats guessing at the format.
//
// build:
//   clang -O2 -o tools/au_probe tools/au_probe.c \
//         -framework AudioToolbox -framework CoreFoundation
//
// usage:
//   au_probe <type> <subtype> <manufacturer> [options]
//     --load FILE      read an XML/binary plist and set it as ClassInfo
//     --save FILE      write the current ClassInfo back to FILE (XML)
//     --dump           print "index<TAB>id<TAB>name<TAB>value" for all parameters
//     --elem N         element used for parameters (default 0)
//     --set ID VALUE   set one parameter before dumping
//     --quiet          suppress instantiation chatter on stderr

#include <AudioToolbox/AudioToolbox.h>
#include <CoreFoundation/CoreFoundation.h>
#include <dispatch/dispatch.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static OSType fourcc(const char *s) {
    return ((OSType)(unsigned char)s[0] << 24) | ((OSType)(unsigned char)s[1] << 16) |
           ((OSType)(unsigned char)s[2] << 8)  |  (OSType)(unsigned char)s[3];
}

static int dump_parameters(AudioUnit au, UInt32 elem) {
    UInt32 size = 0;
    OSStatus st = AudioUnitGetPropertyInfo(au, kAudioUnitProperty_ParameterList,
                                           kAudioUnitScope_Global, 0, &size, NULL);
    if (st != noErr || size == 0) {
        fprintf(stderr, "au_probe: parameter list unavailable (status %d, size %u)\n",
                (int)st, (unsigned)size);
        return 3;
    }
    int n = (int)(size / sizeof(AudioUnitParameterID));
    AudioUnitParameterID *ids = (AudioUnitParameterID *)malloc(size);
    st = AudioUnitGetProperty(au, kAudioUnitProperty_ParameterList,
                              kAudioUnitScope_Global, 0, ids, &size);
    if (st != noErr) {
        fprintf(stderr, "au_probe: parameter list read failed (%d)\n", (int)st);
        free(ids);
        return 4;
    }
    for (int i = 0; i < n; i++) {
        AudioUnitParameterInfo info;
        UInt32 isz = sizeof(info);
        OSStatus s2 = AudioUnitGetProperty(au, kAudioUnitProperty_ParameterInfo,
                                           kAudioUnitScope_Global, ids[i], &info, &isz);
        AudioUnitParameterValue v = 0;
        AudioUnitGetParameter(au, ids[i], kAudioUnitScope_Global, elem, &v);
        if (s2 != noErr) continue;
        // name can be a CFStringRef for some units; info.name is a char[52] for v1
        printf("%d\t%u\t%.*s\t%.9g\n", i, (unsigned)ids[i], 52, info.name, (double)v);
    }
    free(ids);
    return 0;
}

int main(int argc, char **argv) {
    if (argc < 4) {
        fprintf(stderr,
                "usage: %s <type> <subtype> <manufacturer> "
                "[--load FILE] [--save FILE] [--dump] [--elem N] [--set ID VALUE]\n",
                argv[0]);
        return 2;
    }
    AudioComponentDescription d = {0};
    d.componentType         = fourcc(argv[1]);
    d.componentSubType      = fourcc(argv[2]);
    d.componentManufacturer = fourcc(argv[3]);

    const char *load = NULL, *save = NULL;
    int dump = 0, quiet = 0;
    UInt32 elem = 0;
    AudioUnitParameterID setId = 0;
    AudioUnitParameterValue setVal = 0;
    int haveSet = 0;

    for (int i = 4; i < argc; i++) {
        if (!strcmp(argv[i], "--load") && i + 1 < argc)       load = argv[++i];
        else if (!strcmp(argv[i], "--save") && i + 1 < argc)  save = argv[++i];
        else if (!strcmp(argv[i], "--dump"))                   dump = 1;
        else if (!strcmp(argv[i], "--quiet"))                  quiet = 1;
        else if (!strcmp(argv[i], "--elem") && i + 1 < argc)   elem = (UInt32)strtoul(argv[++i], NULL, 0);
        else if (!strcmp(argv[i], "--set") && i + 2 < argc) {
            setId  = (AudioUnitParameterID)strtoul(argv[++i], NULL, 0);
            setVal = (AudioUnitParameterValue)strtod(argv[++i], NULL);
            haveSet = 1;
        }
    }

    AudioComponent comp = AudioComponentFindNext(NULL, &d);
    if (!comp) { fprintf(stderr, "au_probe: component not found\n"); return 1; }

    AudioUnit au = NULL;
    OSStatus st = AudioComponentInstanceNew(comp, &au);
    if (st != noErr) {
        __block AudioUnit oau = NULL;
        __block OSStatus ost = noErr;
        dispatch_semaphore_t sem = dispatch_semaphore_create(0);
        AudioComponentInstantiate(comp, kAudioComponentInstantiation_LoadOutOfProcess,
                                  ^(AudioComponentInstance inst, OSStatus err) {
                                      oau = inst; ost = err;
                                      dispatch_semaphore_signal(sem);
                                  });
        dispatch_semaphore_wait(sem, dispatch_time(DISPATCH_TIME_NOW, 30LL * NSEC_PER_SEC));
        if (ost != noErr || !oau) {
            fprintf(stderr, "au_probe: instantiation failed (%d)\n", (int)ost);
            return 1;
        }
        au = oau;
        if (!quiet) fprintf(stderr, "au_probe: instantiated out-of-process\n");
    }

    st = AudioUnitInitialize(au);
    if (st != noErr && !quiet)
        fprintf(stderr, "au_probe: AudioUnitInitialize status %d (continuing)\n", (int)st);

    if (load) {
        CFURLRef url = CFURLCreateFromFileSystemRepresentation(
            NULL, (const UInt8 *)load, (CFIndex)strlen(load), false);
        CFReadStreamRef rs = CFReadStreamCreateWithFile(NULL, url);
        CFReadStreamOpen(rs);
        CFPropertyListRef pl = NULL;
        CFPropertyListFormat fmt = 0;
        pl = CFPropertyListCreateWithStream(NULL, rs, 0,
                                            kCFPropertyListImmutable, &fmt, NULL);
        CFReadStreamClose(rs);
        CFRelease(rs);
        CFRelease(url);
        if (!pl) { fprintf(stderr, "au_probe: cannot parse %s\n", load); return 5; }
        st = AudioUnitSetProperty(au, kAudioUnitProperty_ClassInfo,
                                  kAudioUnitScope_Global, 0, &pl, sizeof(pl));
        CFRelease(pl);
        if (st != noErr) {
            fprintf(stderr, "au_probe: ClassInfo rejected (%d)\n", (int)st);
            return 6;
        }
        if (!quiet) fprintf(stderr, "au_probe: ClassInfo loaded from %s\n", load);
    }

    if (haveSet) {
        st = AudioUnitSetParameter(au, setId, kAudioUnitScope_Global, elem, setVal, 0);
        AudioUnitParameterValue rb = 0;
        AudioUnitGetParameter(au, setId, kAudioUnitScope_Global, elem, &rb);
        if (!quiet)
            fprintf(stderr, "au_probe: set %u -> %.9g status %d readback %.9g\n",
                    (unsigned)setId, (double)setVal, (int)st, (double)rb);
    }

    int rc = 0;
    if (dump) rc = dump_parameters(au, elem);

    if (save) {
        CFPropertyListRef pl = NULL;
        UInt32 psz = sizeof(pl);
        st = AudioUnitGetProperty(au, kAudioUnitProperty_ClassInfo,
                                  kAudioUnitScope_Global, 0, &pl, &psz);
        if (st != noErr || !pl) {
            fprintf(stderr, "au_probe: ClassInfo read failed (%d)\n", (int)st);
            rc = 7;
        } else {
            CFDataRef xml = CFPropertyListCreateData(NULL, pl,
                                                     kCFPropertyListXMLFormat_v1_0, 0, NULL);
            if (xml) {
                FILE *f = fopen(save, "wb");
                if (f) {
                    fwrite(CFDataGetBytePtr(xml), 1, (size_t)CFDataGetLength(xml), f);
                    fclose(f);
                    if (!quiet) fprintf(stderr, "au_probe: ClassInfo written to %s\n", save);
                } else { rc = 8; }
                CFRelease(xml);
            }
            CFRelease(pl);
        }
    }

    AudioUnitUninitialize(au);
    AudioComponentInstanceDispose(au);
    return rc;
}
