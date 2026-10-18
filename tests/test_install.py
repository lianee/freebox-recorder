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

    def test_paths(self):
        self.assertEqual(self.sh("mount_point_for 'Disque dur'")[1], "/mnt/disque-dur")
        self.assertEqual(self.sh("fstab_escape '//h/Disque dur'")[1], r"//h/Disque\040dur")

    def test_guard(self):
        self.fake("systemctl", "exit 0\n")   # service active
        self.fake("curl", "echo '[]'\n")
        self.assertEqual(self.sh("guard_idle && echo free")[:2], (0, "free\n"))
        self.fake("curl", "echo '[{\"key\": \"x\"}]'\n")
        rc, _, err = self.sh("guard_idle")
        self.assertNotEqual(rc, 0)
        self.assertIn("enregistrement en cours", err)
        self.assertEqual(self.sh("FORCE=1 guard_idle && echo forced")[1], "forced\n")
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


if __name__ == "__main__":
    unittest.main()
