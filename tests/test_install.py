"""install.sh helpers, run in bash with FBXREC_SOURCE_ONLY=1 and fake system commands."""
import os
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL = os.path.join(ROOT, "install.sh")


class InstallHelpers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bin = os.path.join(self.tmp.name, "bin")
        os.mkdir(self.bin)

    def tearDown(self):
        self.tmp.cleanup()

    def fake(self, name, script):
        p = os.path.join(self.bin, name)
        with open(p, "w") as f:
            f.write("#!/bin/sh\n" + script)
        os.chmod(p, 0o755)

    def sh(self, code):
        env = dict(os.environ, FBXREC_SOURCE_ONLY="1", PATH=self.bin + ":" + os.environ["PATH"])
        r = subprocess.run(["bash", "-c", f'source "{INSTALL}"; {code}'], env=env,
                           capture_output=True, text=True, timeout=20)
        return r.returncode, r.stdout, r.stderr

    def test_sourcing_runs_nothing(self):
        self.assertEqual(self.sh("true")[:2], (0, ""))

    def test_freebox_mounts(self):
        # findmnt -r output: spaces escaped as \x20; other CIFS servers ignored
        self.fake("findmnt", r"""cat <<'X'
//mafreebox.freebox.fr/interne /mnt/interne
//MAFREEBOX.freebox.fr/Disque\x20dur /mnt/disque\x20dur
//nas.local/films /mnt/films
X
""")
        rc, out, _ = self.sh("freebox_mounts")
        self.assertEqual((rc, out.splitlines()), (0, ["/mnt/interne", "/mnt/disque dur"]))

    def test_choose_disk_single_mount(self):
        self.fake("findmnt", "echo '//mafreebox.freebox.fr/interne /mnt/interne'\n")
        self.assertEqual(self.sh("choose_disk")[:2], (0, "/mnt/interne"))

    def test_choose_disk_refuses_ambiguity(self):
        self.fake("findmnt", "printf '//mafreebox.freebox.fr/a /mnt/a\\n//mafreebox.freebox.fr/b /mnt/b\\n'\n")
        rc, out, err = self.sh("choose_disk")
        self.assertNotEqual(rc, 0)
        self.assertIn("FBXREC_OUT", err)

    def test_share_name_with_slash_refused(self):
        self.fake("findmnt", "true\n")
        rc, _, err = self.sh("FBXREC_SHARE='../etc' choose_disk")
        self.assertNotEqual(rc, 0)
        self.assertIn("invalide", err)

    def fake_mount(self, ok_share):
        # mount succeeds only for //host/<ok_share>, failing like mount.cifs otherwise
        self.fake("mount", f"""echo "$*" >> "{self.tmp.name}/mounts"
case "$3" in */{ok_share}) exit 0 ;; esac
echo "mount error(2): No such file or directory" >&2
echo "Refer to the mount.cifs(8) manual page" >&2
exit 32
""")
        self.fake("umount", "true\n")

    def test_probe_shares(self):
        self.fake_mount("Freebox")
        self.assertEqual(self.sh("probe_shares")[:2], (0, "Freebox\n"))

    def test_given_share_that_fails_says_why(self):
        self.fake("findmnt", "true\n")
        self.fake_mount("Freebox")
        rc, _, err = self.sh("FBXREC_SHARE=Freebox_Server choose_disk")
        self.assertNotEqual(rc, 0)
        self.assertIn("No such file or directory", err)
        self.assertNotIn("Refer to", err)
        self.assertIn("SMBv2", err)

    def test_asks_again_after_a_bad_share(self):
        # Delta case of 2026-10-19: the server name typed instead of the share name
        self.fake("findmnt", "true\n")
        self.fake_mount("nothing-matches")
        answers = os.path.join(self.tmp.name, "answers")
        with open(answers, "w") as f:
            f.write("Freebox_Server\n\\\\Freebox_Server\\Freebox\n\n")
        rc, _, err = self.sh(f'exec 4<"{answers}"; ask() {{ local a; read -r -u 4 a; printf %s "$a"; }}; choose_disk')
        self.assertNotEqual(rc, 0)
        self.assertIn("aucun disque Freebox trouvé", err)
        self.assertIn("//mafreebox.freebox.fr/Freebox_Server impossible (mount error(2)", err)
        self.assertIn("nom invalide", err)
        self.assertIn("abandon", err)
        with open(os.path.join(self.tmp.name, "mounts")) as f:
            tried = [l.split()[2] for l in f]
        self.assertEqual(tried[-1], "//mafreebox.freebox.fr/Freebox_Server")   # invalid one not tried

    def test_paths(self):
        self.assertEqual(self.sh("mount_point_for 'Disque dur'")[1], "/mnt/disque-dur")
        self.assertEqual(self.sh("fstab_escape '//h/Disque dur'")[1], r"//h/Disque\040dur")

    def busy_curl(self, busy_body, busy_code=200, running="[]"):
        """Fake curl: /api/busy answers busy_body with busy_code (-w adds the code), /api/running `running`."""
        self.fake("curl", f"""for a in "$@"; do u="$a"; done
case "$u" in
  */api/busy*) printf '%s\\n%s' '{busy_body}' '{busy_code}' ;;
  */api/running) echo '{running}' ;;
esac
""")

    def test_guard(self):
        self.fake("systemctl", "exit 0\n")   # service active
        self.busy_curl('{"busy": null}')
        self.assertEqual(self.sh("guard_idle && echo free")[:2], (0, "free\n"))
        self.busy_curl('{"busy": "enregistrement en cours, réessayez après"}')
        rc, _, err = self.sh("guard_idle")
        self.assertNotEqual(rc, 0)
        self.assertIn("enregistrement en cours", err)
        self.busy_curl('{"busy": "un enregistrement commence dans moins de 10 minutes (NOPLP)"}')
        rc, _, err = self.sh("guard_idle")
        self.assertNotEqual(rc, 0)
        self.assertIn("NOPLP", err)                          # one starting soon refuses too
        self.assertEqual(self.sh("FORCE=1 guard_idle && echo forced")[1], "forced\n")
        self.busy_curl("x", 404, "[]")                       # service older than /api/busy
        self.assertEqual(self.sh("guard_idle && echo free")[1], "free\n")
        self.busy_curl("x", 404, '[{"key": "x"}]')
        self.assertNotEqual(self.sh("guard_idle")[0], 0)
        self.busy_curl("not json")                           # unreadable: assume busy
        self.assertNotEqual(self.sh("guard_idle")[0], 0)
        self.busy_curl("x", 500)
        self.assertNotEqual(self.sh("guard_idle")[0], 0)
        self.fake("curl", "exit 7\n")        # active but not answering: assume busy
        self.assertNotEqual(self.sh("guard_idle")[0], 0)
        self.fake("systemctl", "exit 3\n")   # service stopped: nothing to cut
        self.assertEqual(self.sh("guard_idle && echo free")[1], "free\n")

    def test_password_command_with_spaces_in_config(self):
        env_file = os.path.join(self.tmp.name, "fbxrec.env")
        data = os.path.join(self.tmp.name, "data")
        os.mkdir(data)
        with open(env_file, "w") as f:
            f.write(f"FBXREC_OUT=/mnt/disque dur/Enregistrements TV\nFBXREC_DATA={data}\n")
        open(os.path.join(data, "password"), "w").close()
        rc, _, err = self.sh(f'ENV_FILE="{env_file}" APP_DIR="{ROOT}" install_commands "{self.bin}" '
                             f'&& "{self.bin}/fbxrec-password" --remove')
        self.assertEqual(rc, 0, err)
        self.assertNotIn("not found", err)
        self.assertFalse(os.path.exists(os.path.join(data, "password")), "removed from FBXREC_DATA")

    def test_bad_tarball_says_why(self):
        bad = os.path.join(self.tmp.name, "x.tar.gz")
        with open(bad, "w") as f:
            f.write("not a tarball")
        dest = os.path.join(self.tmp.name, "dest")
        os.mkdir(dest)
        rc, _, err = self.sh(f'FBXREC_TARBALL="{bad}" fetch "{dest}"')
        self.assertNotEqual(rc, 0)
        self.assertIn("archive illisible", err)

    def test_download_failure_cleans_up_quietly(self):
        # whole --update path as a script: the EXIT trap must not trip over set -u
        self.fake("id", "echo 0\n")
        self.fake("systemctl", "exit 3\n")
        self.fake("mktemp", f'mkdir -p "{self.tmp.name}/work" && echo "{self.tmp.name}/work"\n')
        env = dict(os.environ, PATH=self.bin + ":" + os.environ["PATH"], LC_ALL="C",
                   FBXREC_TARBALL=os.path.join(self.tmp.name, "missing.tar.gz"))
        r = subprocess.run(["bash", INSTALL, "--update"], env=env, capture_output=True, text=True, timeout=20)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("téléchargement impossible", r.stderr)
        self.assertNotIn("unbound variable", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "work")), "temp dir removed")

    def test_messages_show_elapsed_time(self):
        rc, _, err = self.sh(f'FBXREC_T0=$(( $(date +%s) - 125 )); say hello')
        # the second may tick between the two `date` calls on a loaded machine (seen 2026-10-20: 2:06)
        self.assertIn(err.strip(), ("fbxrec [2:05]: hello", "fbxrec [2:06]: hello"))

    def test_port_80_when_free(self):
        env = os.path.join(self.tmp.name, "fbxrec.env")
        self.fake("ss", "exit 0\n")                           # nothing listens
        rc, _, err = self.sh(f'ENV_FILE="{env}" FBXREC_OUT=/mnt/x write_config')
        self.assertEqual(rc, 0, err)
        with open(env) as f:
            self.assertIn("FBXREC_PORT=80\n", f.read())
        self.assertEqual(self.sh("web_url 192.168.1.22 80")[1], "http://192.168.1.22/")
        self.assertEqual(self.sh("web_url 192.168.1.22 8080")[1], "http://192.168.1.22:8080/")

    def test_port_8080_when_80_is_taken(self):
        env = os.path.join(self.tmp.name, "fbxrec.env")
        self.fake("ss", 'echo "LISTEN 0 511 0.0.0.0:80 0.0.0.0:*"\n')   # a web server already there
        rc, _, err = self.sh(f'ENV_FILE="{env}" FBXREC_OUT=/mnt/x write_config')
        with open(env) as f:
            self.assertIn("FBXREC_PORT=8080\n", f.read())
        self.assertIn("port 80 déjà utilisé", err)
        self.fake("ss", "exit 0\n")                           # an existing configuration keeps its port
        self.sh(f'ENV_FILE="{env}" FBXREC_OUT=/mnt/x write_config')
        with open(env) as f:
            self.assertIn("FBXREC_PORT=8080\n", f.read())

    def test_port_asked(self):
        env = os.path.join(self.tmp.name, "fbxrec.env")
        self.fake("ss", "exit 0\n")
        rc, _, err = self.sh(f'ENV_FILE="{env}" FBXREC_OUT=/mnt/x FBXREC_PORT=8090 write_config')
        self.assertEqual(rc, 0, err)
        with open(env) as f:
            self.assertIn("FBXREC_PORT=8090\n", f.read())
        rc, _, err = self.sh(f'ENV_FILE="{env}" FBXREC_PORT=81 write_config')   # existing install: changed
        self.assertEqual(rc, 0, err)
        with open(env) as f:
            conf = f.read()
        self.assertIn("FBXREC_PORT=81\n", conf)
        self.assertNotIn("8090", conf)
        self.assertIn("changé", err)
        for bad in ("http", "0", "70000", "80 81", "-1"):
            rc, _, err = self.sh(f'ENV_FILE="{env}" FBXREC_PORT="{bad}" write_config')
            self.assertNotEqual(rc, 0, bad)
        self.fake("ss", 'echo "LISTEN 0 511 0.0.0.0:82 0.0.0.0:*"\n')
        rc, _, err = self.sh(f'ENV_FILE="{env}" FBXREC_PORT=82 write_config')
        self.assertNotEqual(rc, 0)
        self.assertIn("déjà utilisé", err)
        with open(env) as f:
            self.assertIn("FBXREC_PORT=81\n", f.read())               # unchanged by the refusals

    def test_branch_source(self):
        dest = os.path.join(self.tmp.name, "dest")
        os.mkdir(dest)
        self.fake("curl", 'echo "$@" > "%s/url"; exit 22\n' % self.tmp.name)
        rc, _, err = self.sh(f'FBXREC_BRANCH=master fetch "{dest}"')
        self.assertNotEqual(rc, 0)
        with open(os.path.join(self.tmp.name, "url")) as f:
            self.assertIn("/archive/refs/heads/master.tar.gz", f.read())
        self.assertIn("branche master", err)
        rc, _, err = self.sh(f'FBXREC_BRANCH="../x; rm" fetch "{dest}"')
        self.assertIn("nom de branche invalide", err)

    def test_package_install_options(self):
        log = os.path.join(self.tmp.name, "apt.log")
        self.fake("dpkg-query", "exit 1\n")                       # nothing installed
        self.fake("apt-get", f'echo "$@" >> "{log}"\n')
        self.fake("find", "echo /var/lib/apt/lists/x_Packages\n")  # lists fresh: no update
        self.assertEqual(self.sh("install_packages")[0], 0)
        with open(log) as f:
            calls = f.read().splitlines()
        self.assertEqual(len(calls), 1, calls)
        self.assertIn("--no-install-recommends", calls[0])
        self.assertNotIn("--force-unsafe-io", calls[0])
        self.assertIn("ffmpeg", calls[0])
        os.remove(log)
        self.fake("find", "true\n")                                # stale lists: update first
        self.sh("install_packages")
        with open(log) as f:
            self.assertTrue(f.read().startswith("update"))

    def test_selftest(self):
        out = os.path.join(self.tmp.name, "out")
        os.mkdir(out)
        self.fake("sudo", 'shift 2; exec "$@"\n')     # sudo -u USER cmd...
        self.fake("timeout", 'shift; exec "$@"\n')
        self.fake("ffmpeg", 'for a; do last=$a; done; head -c 200000 /dev/zero > "$last"\n')
        rc, _, err = self.sh(f'selftest me "{out}"')
        self.assertIn("contrôle OK : 195 Ko", err)
        self.assertEqual(os.listdir(out), [], "test file removed")
        self.fake("ffmpeg", "exit 1\n")                 # no stream: warn, do not abort
        rc, _, err = self.sh(f'selftest me "{out}" && echo continued')
        self.assertIn("ATTENTION", err)
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
