/*
 * au_classinfo.c — ask an Audio Unit to serialise ITSELF.
 *
 * Why: every plug-in has its own private state container.  Rather than reverse
 * engineer each one, instantiate the plug-in, let it write the state through
 * kAudioUnitProperty_ClassInfo, and use that blob.  This is "L2" in the kit.
 *
 * Build:
 *   clang -O2 -o tools/au_classinfo tools/au_classinfo.c \
 *         -framework AudioToolbox -framework CoreFoundation
 *
 * Use:
 *   ./tools/au_classinfo aumf FQ4p FabF --out /tmp/q4.plist
 *   ./tools/au_classinfo aumf FQ4p FabF --set 7 350 --out /tmp/q4b.plist
 *
 * Output: the saved XML plist, plus a one-line summary of its top-level keys
 * and the byte length of every data value.
 */
#include <AudioToolbox/AudioToolbox.h>
#include <CoreFoundation/CoreFoundation.h>
#include <dispatch/dispatch.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static AudioComponentInstance instantiate(AudioComponent comp) {
    AudioComponentInstance au = NULL;
    OSStatus st = AudioComponentInstanceNew(comp, &au);
    if (st == noErr && au) return au;
    fprintf(stderr, "# in-process failed (%d); trying out of process\n", (int)st);
    dispatch_semaphore_t sem = dispatch_semaphore_create(0);
    __block AudioComponentInstance got = NULL;
    __block OSStatus gst = noErr;
    AudioComponentInstantiate(comp, kAudioComponentInstantiation_LoadOutOfProcess,
                              ^(AudioComponentInstance inst, OSStatus s) {
        got = inst; gst = s; dispatch_semaphore_signal(sem);
    });
    dispatch_semaphore_wait(sem, dispatch_time(DISPATCH_TIME_NOW, 30ull * NSEC_PER_SEC));
    if (got) return got;
    fprintf(stderr, "# out-of-process failed (%d)\n", (int)gst);
    return NULL;
}

static UInt32 parse_fourcc(const char *s) {
    if (strlen(s) != 4) { fprintf(stderr, "fourcc must be 4 chars: %s\n", s); exit(2); }
    return ((UInt32)(unsigned char)s[0] << 24) | ((UInt32)(unsigned char)s[1] << 16)
         | ((UInt32)(unsigned char)s[2] << 8)  |  (UInt32)(unsigned char)s[3];
}

static void describe(CFPropertyListRef pl) {
    if (!pl) { printf("# ClassInfo is NULL\n"); return; }
    CFTypeID tid = CFGetTypeID(pl);
    if (tid == CFDictionaryGetTypeID()) {
        CFDictionaryRef d = (CFDictionaryRef)pl;
        CFIndex n = CFDictionaryGetCount(d);
        printf("# ClassInfo is a dictionary, %ld keys\n", (long)n);
        const void **keys = malloc(sizeof(void*) * (size_t)n);
        const void **vals = malloc(sizeof(void*) * (size_t)n);
        CFDictionaryGetKeysAndValues(d, keys, vals);
        for (CFIndex i = 0; i < n; i++) {
            char kb[256] = "?";
            if (CFGetTypeID(keys[i]) == CFStringGetTypeID())
                CFStringGetCString((CFStringRef)keys[i], kb, sizeof(kb), kCFStringEncodingUTF8);
            CFTypeRef v = vals[i];
            CFTypeID vt = CFGetTypeID(v);
            if (vt == CFDataGetTypeID())
                printf("#   %-28s data  %ld bytes\n", kb, (long)CFDataGetLength((CFDataRef)v));
            else if (vt == CFStringGetTypeID()) {
                char vb[256] = "";
                CFStringGetCString((CFStringRef)v, vb, sizeof(vb), kCFStringEncodingUTF8);
                printf("#   %-28s string %s\n", kb, vb);
            } else if (vt == CFNumberGetTypeID()) {
                long long iv = 0; CFNumberGetValue((CFNumberRef)v, kCFNumberLongLongType, &iv);
                printf("#   %-28s num    %lld\n", kb, iv);
            } else if (vt == CFBooleanGetTypeID())
                printf("#   %-28s bool   %d\n", kb, CFBooleanGetValue((CFBooleanRef)v));
            else if (vt == CFDictionaryGetTypeID())
                printf("#   %-28s dict   %ld keys\n", kb, (long)CFDictionaryGetCount((CFDictionaryRef)v));
            else if (vt == CFArrayGetTypeID())
                printf("#   %-28s array  %ld items\n", kb, (long)CFArrayGetCount((CFArrayRef)v));
            else
                printf("#   %-28s other  type %lu\n", kb, (unsigned long)vt);
        }
        free(keys); free(vals);
    } else {
        printf("# ClassInfo type id %lu (not a dictionary)\n", (unsigned long)tid);
    }
}

int main(int argc, char **argv) {
    if (argc < 4) {
        fprintf(stderr, "usage: %s <type> <subtype> <manufacturer>"
                        " [--set <id> <value>] [--out <path.plist>]\n", argv[0]);
        return 2;
    }
    AudioComponentDescription desc; memset(&desc, 0, sizeof(desc));
    desc.componentType = parse_fourcc(argv[1]);
    desc.componentSubType = parse_fourcc(argv[2]);
    desc.componentManufacturer = parse_fourcc(argv[3]);

    long set_id = -1; double set_val = 0; const char *out = NULL; UInt32 elem = 0;
    int render = 0, presets = 0, preset = -1;
    for (int i = 4; i < argc; i++) {
        if (!strcmp(argv[i], "--set") && i + 2 < argc) {
            set_id = strtol(argv[++i], NULL, 0); set_val = strtod(argv[++i], NULL);
        } else if (!strcmp(argv[i], "--out") && i + 1 < argc) out = argv[++i];
        else if (!strcmp(argv[i], "--elem") && i + 1 < argc) elem = (UInt32)strtol(argv[++i], NULL, 0);
        else if (!strcmp(argv[i], "--render")) render = 1;
        else if (!strcmp(argv[i], "--presets")) presets = 1;
        else if (!strcmp(argv[i], "--preset") && i + 1 < argc) preset = (int)strtol(argv[++i], NULL, 0);
    }

    AudioComponent comp = AudioComponentFindNext(NULL, &desc);
    if (!comp) { fprintf(stderr, "component not found\n"); return 1; }
    AudioComponentInstance au = instantiate(comp);
    if (!au) return 1;

    CFStringRef name = NULL;
    if (AudioComponentCopyName(comp, &name) == noErr && name) {
        char b[256]; CFStringGetCString(name, b, sizeof(b), kCFStringEncodingUTF8);
        printf("# %s\n", b); CFRelease(name);
    }
    if (AudioUnitInitialize(au) != noErr)
        fprintf(stderr, "# warning: AudioUnitInitialize failed\n");

    if (set_id >= 0) {
        OSStatus s = AudioUnitSetParameter(au, (AudioUnitParameterID)set_id,
                                           kAudioUnitScope_Global, elem, (AudioUnitParameterValue)set_val, 0);
        AudioUnitParameterValue back = -12345;
        OSStatus rs = AudioUnitGetParameter(au, (AudioUnitParameterID)set_id,
                                            kAudioUnitScope_Global, elem, &back);
        printf("# set param %ld = %g on elem %u -> status=%d readback(status=%d)=%g\n",
               set_id, set_val, (unsigned)elem, (int)s, (int)rs, (double)back);
    }

    if (presets) {
        CFArrayRef fp = NULL; UInt32 fsz = sizeof(fp);
        OSStatus st2 = AudioUnitGetProperty(au, kAudioUnitProperty_FactoryPresets,
                                            kAudioUnitScope_Global, 0, &fp, &fsz);
        printf("# FactoryPresets status=%d\n", (int)st2);
        if (st2 == noErr && fp) {
            CFIndex n = CFArrayGetCount(fp);
            printf("# %ld presets\n", (long)n);
            for (CFIndex i = 0; i < n && i < 40; i++) {
                AUPreset *pr = (AUPreset *)CFArrayGetValueAtIndex(fp, i);
                char nm[256] = "?";
                if (pr->presetName) CFStringGetCString(pr->presetName, nm, sizeof(nm), kCFStringEncodingUTF8);
                printf("#   [%ld] %s\n", (long)i, nm);
            }
        }
    }
    if (preset >= 0) {
        CFArrayRef fp = NULL; UInt32 fsz = sizeof(fp);
        if (AudioUnitGetProperty(au, kAudioUnitProperty_FactoryPresets,
                                 kAudioUnitScope_Global, 0, &fp, &fsz) == noErr && fp) {
            AUPreset *pr = (AUPreset *)CFArrayGetValueAtIndex(fp, preset);
            AUPreset cp = { .presetNumber = pr->presetNumber, .presetName = pr->presetName };
            OSStatus st3 = AudioUnitSetProperty(au, kAudioUnitProperty_PresentPreset,
                                                kAudioUnitScope_Global, 0, &cp, sizeof(cp));
            char nm[256] = "?";
            if (pr->presetName) CFStringGetCString(pr->presetName, nm, sizeof(nm), kCFStringEncodingUTF8);
            printf("# load preset [%d] %s -> %d\n", preset, nm, (int)st3);
        }
    }
    if (render) {
        UInt32 mfps = 512;
        AudioUnitSetProperty(au, kAudioUnitProperty_MaximumFramesPerSlice,
                             kAudioUnitScope_Global, 0, &mfps, sizeof(mfps));
        enum { CH = 2, FR = 512 };
        float *buf = calloc((size_t)CH * FR, sizeof(float));
        AudioBufferList *abl = calloc(1, sizeof(AudioBufferList) + (CH - 1) * sizeof(AudioBuffer));
        abl->mNumberBuffers = CH;
        for (int c = 0; c < CH; c++) {
            abl->mBuffers[c].mNumberChannels = 1;
            abl->mBuffers[c].mDataByteSize = FR * sizeof(float);
            abl->mBuffers[c].mData = buf + (size_t)c * FR;
        }
        OSStatus rst = AudioUnitRender(au, NULL, NULL, 0, FR, abl);
        printf("# render pass -> %d\n", (int)rst);
        free(abl); free(buf);
    }

    UInt32 sz = 0; Boolean wr = false;
    OSStatus ps = AudioUnitGetPropertyInfo(au, kAudioUnitProperty_ClassInfo,
                                           kAudioUnitScope_Global, 0, &sz, &wr);
    printf("# ClassInfo GetPropertyInfo status=%d size=%u writable=%d\n", (int)ps, sz, (int)wr);

    CFPropertyListRef pl = NULL;
    UInt32 s = sizeof(pl);
    OSStatus st = AudioUnitGetProperty(au, kAudioUnitProperty_ClassInfo,
                                       kAudioUnitScope_Global, 0, &pl, &s);
    printf("# ClassInfo GetProperty status=%d\n", (int)st);
    if (st != noErr || !pl) {
        AudioUnitUninitialize(au); AudioComponentInstanceDispose(au); return 1;
    }
    describe(pl);

    if (out) {
        CFDataRef xml = CFPropertyListCreateData(kCFAllocatorDefault, pl,
                                                 kCFPropertyListXMLFormat_v1_0, 0, NULL);
        if (xml) {
            FILE *f = fopen(out, "wb");
            if (f) { fwrite(CFDataGetBytePtr(xml), 1, (size_t)CFDataGetLength(xml), f); fclose(f); }
            printf("# wrote %ld bytes to %s\n", (long)CFDataGetLength(xml), out);
            CFRelease(xml);
        }
    }
    CFRelease(pl);
    AudioUnitUninitialize(au);
    AudioComponentInstanceDispose(au);
    return 0;
}
