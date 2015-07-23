package main

import (
    "errors"
    "io/ioutil"
    "os"
    "path"
    "strings"
    "unicode/utf8"
)

func EnsureGutFolders(ctx *SyncContext) (err error) {
    _, err = ctx.Output("mkdir", "-p", ctx.AbsPath(GutSrcPath))
    if err != nil { return err }
    _, err = ctx.Output("mkdir", "-p", ctx.AbsPath(GutWinSrcPath))
    if err != nil { return err }
    _, err = ctx.Output("mkdir", "-p", ctx.AbsPath(GutDistPath))
    return err
}

func renameGitToGut(s string) string {
    s = strings.Replace(s, "git", "gut", -1)
    s = strings.Replace(s, "Git", "Gut", -1)
    s = strings.Replace(s, "GIT", "GUT", -1)
    s = strings.Replace(s, "digut", "digit", -1)
    s = strings.Replace(s, "DIGUT", "DIGIT", -1)
    return s
}

func rewriteGitToGutRecursive(pathRoot string) (err error) {
    entries, err := ioutil.ReadDir(pathRoot)
    if err != nil { return err }
    for _, entry := range entries {
        origName := entry.Name()
        // Don't modify .gitignore, and don't recurse into .git
        if len(origName) >= 4 && origName[:4] == ".git" { continue }
        name := renameGitToGut(origName)
        p := path.Join(pathRoot, name)
        if (origName != name) {
            os.Rename(path.Join(pathRoot, origName), p)
        }
        if entry.IsDir() {
            err = rewriteGitToGutRecursive(p)
            if err != nil { return err }
        } else {
            _origContents, err := ioutil.ReadFile(p)
            if err != nil { return err }
            if !utf8.Valid(_origContents) {
                continue
            }
            origContents := string(_origContents)
            contents := renameGitToGut(origContents)
            if name == "read-cache.c" {
                // This is a special case super-optimized string parse for the 'i' in 'git':
                contents = strings.Replace(contents, "rest[1] != 'i' && rest[1] != 'I'", "rest[1] != 'u' && rest[1] != 'U'", -1)
            }
            if name == "utf8.c" {
                contents = strings.Replace(contents, "if (c != 'i' && c != 'I'", "if (c != 'u' && c != 'U'", -1)
            }
            if name == "GUT-VERSION-GEN" {
                // GUT-VERSION-GEN attempts to use `git` to look at the git repo's history in order to determine the version string.
                // This prevents gut-gui/GUT-VERSION-GEN from calling `gut` and causing `gut_proxy` to recursively build `gut` in an infinite loop.
                contents = strings.Replace(contents, "gut ", "git ", -1)
            }
            if origContents != contents {
                err = ioutil.WriteFile(p, []byte(contents), os.FileMode(0644))
                if err != nil { return err }
            }
        }
    }
    return nil
}

func RewriteGitToGut(local *SyncContext, pathRoot string) (err error) {
    status := local.NewLogger("rewrite")
    defer status.Close()
    status.Printf("@(dim:Rewriting git to gut...)")
    err = rewriteGitToGutRecursive(local.AbsPath(pathRoot))
    if err != nil {
        status.Printf("@(error: failed)@(dim:.)\n")
        return err
    }
    status.Printf("@(dim: done.)\n")
    return nil
}

func GitHardResetAndClean(local *SyncContext, localPath string, repoUrl string, version string) (err error) {
    // Do a little sanity-check to make sure we're not running these (destructive) operations in some other repo:
    gitRemoteOut, err := local.OutputCwd(localPath, "git", "remote", "-v")
    if err != nil { return err }
    if !strings.Contains(gitRemoteOut, repoUrl) {
        return errors.New("I think I might be trying to git-reset the wrong repo.")
    }
    err = local.QuoteCwd("git-reset", localPath, "git", "reset", "--quiet", "--hard", version)
    if err != nil { return err }
    err = local.QuoteCwd("git-clean", localPath, "git", "clean", "-fdxq")
    return err
}

func GitCloneUpdate(local *SyncContext, localPath string, repoUrl string, version string) (err error) {
    status := local.NewLogger("")
    defer status.Close()
    err = EnsureGutFolders(local)
    if err != nil { return err }
    exists, err := local.PathExists(path.Join(localPath, ".git"))
    if err != nil { return err }
    if !exists {
        p := local.AbsPath(localPath)
        status.Printf("@(dim:Cloning) %s @(dim:into) @(path:%s)@(dim:...)\n", repoUrl, p)
        err = local.Quote("git-clone", "git", "clone", "--progress", repoUrl, p)
        if err != nil { return err }
    }
    // Prevent windows from checking out CRLF line endings and then syncing them to a linux box, which subsequently
    // runs into weird errors due to the CRLFs:
    _, err = local.OutputCwd(localPath, "git", "config", "core.autocrlf", "false")
    if err != nil { return err }
    _, _, retCode, err := local.RunCwd(localPath, "git", "rev-parse", version)
    if retCode != 0 {
        status.Printf("@(dim:Fetching latest from) %s @(dim:in order to upgrade to) %s @(dim:...)", repoUrl, version)
        err = local.Quote("git-fetch", "git", "fetch")
        if err != nil { return err }
        status.Printf("@(dim: done.)\n")
    }
    status.Printf("@(dim:Checking out) %s @(dim:...)", version)
    err = GitHardResetAndClean(local, localPath, repoUrl, version)
    if err != nil { return err }
    status.Printf("@(dim: done.)\n")
    return nil
}

func GutBuildPrepare(local *SyncContext, ctx *SyncContext) (err error) {
    if ctx.IsWindows() {
        err = GitCloneUpdate(local, MsysgitPath, MsysgitRepoUrl, MsysgitVersion)
        if err != nil { return err }
        err = GitCloneUpdate(local, GutWinSrcPath, GitWinRepoUrl, GitWinVersion)
        if err != nil { return err }
        err = RewriteGitToGut(local, GutWinSrcPath)
    } else {
        err = GitCloneUpdate(local, GutSrcPath, GitRepoUrl, GitVersion)
        if err != nil { return err }
        err = RewriteGitToGut(local, GutSrcPath)
    }
    return err
}
