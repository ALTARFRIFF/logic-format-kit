/*
 * au_dump.c — list an Audio Unit's parameters, in the AU's own order.
 *
 * Why this exists: a FabFilter (or Brainworx, or SPL) plug-in stores its state
 * in Logic as a flat array of floats, and the only way to know which float is
 * which parameter is to ask the plug-in.  Logic exposes that through its
 * control-surface protocol, which is slow, unreliable, and needs Logic open.
 * The AudioToolbox API gives the same information directly.
 *
 * Build:
 *   clang -O2 -o au_dump au_dump.c -framework AudioToolbox -framework CoreFoundation
 *
 * Use:
 *   ./au_dump aumf FQ4p FabF            # every parameter of Pro-Q 4
 *   ./au_dump aumf FC3p FabF --values   # ... with each parameter's current value
 *   ./au_dump aufx SPTP SPL1 --values
 *   ./au_dump aumf FQ4p FabF --string 7 350.0
 *                                       # what does the plug-in DISPLAY for
 *                                       # parameter id 7 at value 350.0?
 *
 * The last form is the useful one for decoding an encoding: set a value, ask
 * the plug-in what it calls it, and compare that with the float in the file.
 */

#include <AudioToolbox/AudioToolbox.h>
#include <CoreFoundation/CoreFoundation.h>
#include <dispatch/dispatch.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* Some vendors (Plugin Alliance / brainworx / SPL, and anything iLok-protected)
   refuse AudioComponentInstanceNew(-3000) and only come up outside the host
   process.  Try in-process first, then out-of-process. */
static AudioComponentInstance instantiate(AudioComponent comp) {
    AudioComponentInstance au = NULL;
    OSStatus st = AudioComponentInstanceNew(comp, &au);
    if (st == noErr && au) return au;

    fprintf(stderr, "# in-process instantiation failed (%d); trying out of process\n",
            (int)st);
    dispatch_semaphore_t sem = dispatch_semaphore_create(0);
    __block AudioComponentInstance got = NULL;
    __block OSStatus gst = noErr;
    AudioComponentInstantiate(comp, kAudioComponentInstantiation_LoadOutOfProcess,
                              ^(AudioComponentInstance inst, OSStatus s) {
        got = inst;
        gst = s;
        dispatch_semaphore_signal(sem);
    });
    dispatch_semaphore_wait(sem, dispatch_time(DISPATCH_TIME_NOW, 30ull * NSEC_PER_SEC));
    if (got) return got;
    fprintf(stderr, "# out-of-process instantiation also failed (%d)\n", (int)gst);
    return NULL;
}

static void fourcc(UInt32 v, char *buf) {
    buf[0] = (char)((v >> 24) & 255);
    buf[1] = (char)((v >> 16) & 255);
    buf[2] = (char)((v >> 8) & 255);
    buf[3] = (char)(v & 255);
    buf[4] = 0;
}

static UInt32 parse_fourcc(const char *s) {
    if (strlen(s) != 4) { fprintf(stderr, "fourcc must be 4 chars: %s\n", s); exit(2); }
    return ((UInt32)(unsigned char)s[0] << 24) | ((UInt32)(unsigned char)s[1] << 16)
         | ((UInt32)(unsigned char)s[2] << 8)  |  (UInt32)(unsigned char)s[3];
}

/* The human-readable string the plug-in shows for a parameter value. */
static int string_for_value(AudioUnit au, AudioUnitParameterID pid,
                            AudioUnitParameterValue value, char *out, size_t n) {
    AudioUnitParameterStringFromValue sfv;
    sfv.inParamID = pid;
    sfv.inValue = &value;
    sfv.outString = NULL;
    UInt32 size = sizeof(sfv);
    OSStatus st = AudioUnitGetProperty(au, kAudioUnitProperty_ParameterStringFromValue,
                                       kAudioUnitScope_Global, 0, &sfv, &size);
    if (st != noErr || sfv.outString == NULL) { snprintf(out, n, "(no string)"); return -1; }
    CFStringGetCString(sfv.outString, out, (CFIndex)n, kCFStringEncodingUTF8);
    CFRelease(sfv.outString);
    return 0;
}

int main(int argc, char **argv) {
    if (argc < 4) {
        fprintf(stderr, "usage: %s <type> <subtype> <manufacturer> [--values]"
                        " [--string <id> <value>]\n", argv[0]);
        return 2;
    }
    AudioComponentDescription desc;
    memset(&desc, 0, sizeof(desc));
    desc.componentType = parse_fourcc(argv[1]);
    desc.componentSubType = parse_fourcc(argv[2]);
    desc.componentManufacturer = parse_fourcc(argv[3]);

    int show_values = 0, tsv = 0;
    long want_id = -1;
    double want_value = 0;
    for (int i = 4; i < argc; i++) {
        if (!strcmp(argv[i], "--values")) show_values = 1;
        else if (!strcmp(argv[i], "--tsv")) tsv = 1;
        else if (!strcmp(argv[i], "--string") && i + 2 < argc) {
            want_id = strtol(argv[++i], NULL, 0);
            want_value = strtod(argv[++i], NULL);
        }
    }

    AudioComponent comp = AudioComponentFindNext(NULL, &desc);
    if (!comp) { fprintf(stderr, "component not found\n"); return 1; }

    AudioComponentInstance au = instantiate(comp);
    if (!au) return 1;
    OSStatus st = noErr;

    CFStringRef name = NULL;
    if (AudioComponentCopyName(comp, &name) == noErr && name) {
        char buf[256];
        CFStringGetCString(name, buf, sizeof(buf), kCFStringEncodingUTF8);
        printf("# %s\n", buf);
        CFRelease(name);
    }

    /* v2 AUs report their parameter tree through the Global scope.  Initialise
       so the plug-in has a live sample rate; no render callback is installed. */
    if (AudioUnitInitialize(au) != noErr)
        fprintf(stderr, "# warning: AudioUnitInitialize failed\n");

    if (want_id >= 0) {
        char s[512];
        if (string_for_value(au, (AudioUnitParameterID)want_id, want_value, s, sizeof(s)) == 0)
            printf("%ld\t%g\t%s\n", want_id, want_value, s);
        AudioUnitUninitialize(au);
        AudioComponentInstanceDispose(au);
        return 0;
    }

    UInt32 size = 0;
    Boolean writable = false;
    st = AudioUnitGetPropertyInfo(au, kAudioUnitProperty_ParameterList,
                                  kAudioUnitScope_Global, 0, &size, &writable);
    if (st != noErr || size == 0) {
        fprintf(stderr, "no parameter list (status %d, size %u)\n", (int)st, size);
        AudioUnitUninitialize(au);
        AudioComponentInstanceDispose(au);
        return 1;
    }

    UInt32 count = size / sizeof(AudioUnitParameterID);
    AudioUnitParameterID *ids = malloc(size);
    if (AudioUnitGetProperty(au, kAudioUnitProperty_ParameterList,
                             kAudioUnitScope_Global, 0, ids, &size) != noErr) {
        fprintf(stderr, "parameter list read failed\n");
        return 1;
    }

    if (!tsv) {
        printf("# %u parameters\n", count);
        printf("# %-4s %-5s %-28s %-14s %10s %10s %10s  %s\n",
               "idx", "id", "name", "unit", "min", "max", "default",
               show_values ? "value / display" : "");
    }
    for (UInt32 i = 0; i < count; i++) {
        AudioUnitParameterInfo info;
        UInt32 isize = sizeof(info);
        memset(&info, 0, sizeof(info));
        if (AudioUnitGetProperty(au, kAudioUnitProperty_ParameterInfo,
                                 kAudioUnitScope_Global, ids[i], &info, &isize) != noErr)
            continue;
        char unit[16] = "";
        fourcc(info.unit, unit);
        char extra[600] = "";
        if (show_values) {
            AudioUnitParameterValue v = 0;
            char s[512] = "";
            int got = (AudioUnitGetParameter(au, ids[i], kAudioUnitScope_Global, 0, &v) == noErr);
            if (got) string_for_value(au, ids[i], v, s, sizeof(s));
            snprintf(extra, sizeof(extra), "%g / %s", got ? (double)v : 0.0, s);
        }
        if (tsv) {
            AudioUnitParameterValue cur = 0; int got = 0;
            if (show_values)
                got = (AudioUnitGetParameter(au, ids[i], kAudioUnitScope_Global, 0, &cur) == noErr);
            printf("%u\t%u\t%s\t%g\t%g\t%g", i, (unsigned)ids[i], info.name,
                   (double)info.minValue, (double)info.maxValue, (double)info.defaultValue);
            if (show_values) printf("\t%s\t%g", got ? "1" : "0", got ? (double)cur : 0.0);
            printf("\n");
        } else {
            printf("  %-4u %-5u %-28s %-14s %10g %10g %10g  %s\n",
                   i, (unsigned)ids[i], info.name, unit,
                   (double)info.minValue, (double)info.maxValue,
                   (double)info.defaultValue, extra);
        }
    }

    AudioUnitUninitialize(au);
    AudioComponentInstanceDispose(au);
    free(ids);
    return 0;
}
