// SPDX-License-Identifier: Apache-2.0
//
// feature-macros: find preprocessor feature checks that do not use
// kernel-style CONFIG_* macros. Lustre gates code on autoconf-detected
// macros (HAVE_SERVER_SUPPORT etc.) force-included from the generated
// config.h; the kernel convention is CONFIG_* instead.
//
// A conditional test (#ifdef, #ifndef, #elifdef, #elifndef or the
// defined() operator) in the lustre tree is flagged when the tested
// macro is:
//
//   - named HAVE_* (the autoconf convention), wherever it is defined, or
//   - defined only by the generated config.h, or
//   - never defined at all (a configure-time feature left disabled).
//
// CONFIG_* macros and reserved names (leading underscore: __KERNEL__,
// __GNUC__, ...) are always allowed, as are macros defined in real
// source files (header guards, compat macros, kernel headers) or on the
// command line (MODULE, KBUILD_*).

#include "lint.h"

#include "clang/Basic/SourceManager.h"
#include "clang/Frontend/CompilerInstance.h"
#include "clang/Lex/MacroInfo.h"
#include "clang/Lex/PPCallbacks.h"
#include "clang/Lex/Preprocessor.h"
#include <map>
#include <memory>
#include <mutex>
#include <set>
#include <string>

using namespace clang;

namespace {

// How the tested macro was defined at the point of the test.
enum class DefClass {
  Undefined, // not defined (yet); resolved against the global def sets
  Source,    // defined in a real source file
  Config,    // defined in the generated lustre-release/config.h
  Cmdline,   // predefined or -D on the command line
};

struct TestSite {
  std::string Name;
  std::string Filename;
  unsigned Line;
  DefClass Class;
};

class FeatureMacrosLint : public Lint {
public:
  llvm::StringRef name() const override { return "feature-macros"; }
  std::unique_ptr<TUHandler> makeTUHandler() override;
  void report() override;

  // Cross-TU state, merged under Mutex at each TU's EndOfMainFile.
  // Sites key: "filename:line:name", so the same test reached from many
  // TUs merges to one finding.
  std::mutex Mutex;
  std::map<std::string, TestSite> AllSites;
  std::set<std::string> SourceDefs;
  std::set<std::string> CmdlineDefs;
};

bool startsWith(llvm::StringRef S, llvm::StringRef Prefix) {
  return S.size() >= Prefix.size() && S.substr(0, Prefix.size()) == Prefix;
}

// The autoconf-generated header sits at the top of the lustre tree and is
// force-included into every TU via "-include $PWD/config.h".
bool isGeneratedConfigHeader(llvm::StringRef Path) {
  return inLustreTree(Path) && stripLustrePrefix(Path) == "config.h";
}

class FeatureTracker : public PPCallbacks {
public:
  FeatureTracker(const SourceManager &SM, FeatureMacrosLint &L)
      : SM(SM), L(L) {}

  void MacroDefined(const Token &MacroNameTok,
                    const MacroDirective *MD) override {
    if (!MD || !MacroNameTok.getIdentifierInfo())
      return;
    const MacroInfo *MI = MD->getMacroInfo();
    if (!MI)
      return;
    StringRef Name = MacroNameTok.getIdentifierInfo()->getName();
    switch (classifyDefinition(MI)) {
    case DefClass::Source:
      LocalSourceDefs.insert(Name.str());
      break;
    case DefClass::Cmdline:
      LocalCmdlineDefs.insert(Name.str());
      break;
    default:
      break;
    }
  }

  void Defined(const Token &MacroNameTok, const MacroDefinition &MD,
               SourceRange /*Range*/) override {
    recordTest(MacroNameTok, MD);
  }

  void Ifdef(SourceLocation /*Loc*/, const Token &MacroNameTok,
             const MacroDefinition &MD) override {
    recordTest(MacroNameTok, MD);
  }

  void Ifndef(SourceLocation /*Loc*/, const Token &MacroNameTok,
              const MacroDefinition &MD) override {
    recordTest(MacroNameTok, MD);
  }

  void Elifdef(SourceLocation /*Loc*/, const Token &MacroNameTok,
               const MacroDefinition &MD) override {
    recordTest(MacroNameTok, MD);
  }

  void Elifndef(SourceLocation /*Loc*/, const Token &MacroNameTok,
                const MacroDefinition &MD) override {
    recordTest(MacroNameTok, MD);
  }

  void EndOfMainFile() override {
    std::unique_lock<std::mutex> LockGuard(L.Mutex);
    for (auto &KV : LocalSites) {
      auto it = L.AllSites.find(KV.first);
      if (it == L.AllSites.end())
        L.AllSites.emplace(KV.first, KV.second);
      else if (it->second.Class == DefClass::Undefined)
        it->second.Class = KV.second.Class;
    }
    L.SourceDefs.insert(LocalSourceDefs.begin(), LocalSourceDefs.end());
    L.CmdlineDefs.insert(LocalCmdlineDefs.begin(), LocalCmdlineDefs.end());
  }

private:
  DefClass classifyDefinition(const MacroInfo *MI) {
    if (MI->isBuiltinMacro())
      return DefClass::Cmdline;
    SourceLocation Loc = MI->getDefinitionLoc();
    if (Loc.isInvalid())
      return DefClass::Cmdline;
    FileID FID = SM.getFileID(Loc);
    // The <built-in> and <command line> buffers have no file entry.
    if (FID.isInvalid() || !SM.getFileEntryRefForID(FID).has_value())
      return DefClass::Cmdline;
    StringRef RawFilename = SM.getFilename(Loc);
    if (RawFilename.empty())
      return DefClass::Cmdline;
    if (isGeneratedConfigHeader(canonicalizePath(SM, FID, RawFilename)))
      return DefClass::Config;
    return DefClass::Source;
  }

  void recordTest(const Token &MacroNameTok, const MacroDefinition &MD) {
    if (!MacroNameTok.getIdentifierInfo())
      return;
    StringRef Name = MacroNameTok.getIdentifierInfo()->getName();
    // Kernel-style configuration macros are what we want.
    if (startsWith(Name, "CONFIG_"))
      return;
    // Reserved names: compiler, platform and libc territory.
    if (startsWith(Name, "_"))
      return;
    SourceLocation Loc = MacroNameTok.getLocation();
    if (Loc.isInvalid() || SM.isInSystemHeader(Loc))
      return;
    FileID FID = SM.getFileID(Loc);
    if (FID.isInvalid() || !SM.getFileEntryRefForID(FID).has_value())
      return;
    StringRef RawFilename = SM.getFilename(Loc);
    if (RawFilename.empty())
      return;
    std::string Filename = canonicalizePath(SM, FID, RawFilename);
    if (!inLustreTree(Filename) || isGeneratedConfigHeader(Filename))
      return;
    unsigned Line = SM.getSpellingLineNumber(Loc);

    DefClass Class = MD ? classifyDefinition(MD.getMacroInfo())
                        : DefClass::Undefined;

    std::string Key;
    Key.reserve(Filename.size() + Name.size() + 16);
    Key.append(Filename);
    Key.push_back(':');
    Key.append(std::to_string(Line));
    Key.push_back(':');
    Key.append(Name.data(), Name.size());

    auto it = LocalSites.find(Key);
    if (it == LocalSites.end())
      LocalSites.emplace(std::move(Key),
                         TestSite{Name.str(), std::move(Filename), Line,
                                  Class});
    else if (it->second.Class == DefClass::Undefined)
      it->second.Class = Class;
  }

  const SourceManager &SM;
  FeatureMacrosLint &L;
  std::map<std::string, TestSite> LocalSites;
  std::set<std::string> LocalSourceDefs;
  std::set<std::string> LocalCmdlineDefs;
};

class FeatureMacrosTUHandler : public TUHandler {
public:
  FeatureMacrosTUHandler(FeatureMacrosLint &L) : L(L) {}

  void registerPPCallbacks(CompilerInstance &CI) override {
    CI.getPreprocessor().addPPCallbacks(
        std::make_unique<FeatureTracker>(CI.getSourceManager(), L));
  }

private:
  FeatureMacrosLint &L;
};

std::unique_ptr<TUHandler> FeatureMacrosLint::makeTUHandler() {
  return std::make_unique<FeatureMacrosTUHandler>(*this);
}

void FeatureMacrosLint::report() {
  for (auto &KV : AllSites) {
    TestSite &S = KV.second;
    bool Improper;

    if (startsWith(S.Name, "HAVE_")) {
      // The autoconf naming convention is a feature check no matter
      // where the macro ends up defined.
      Improper = true;
    } else {
      switch (S.Class) {
      case DefClass::Config:
        Improper = true;
        break;
      case DefClass::Source:
      case DefClass::Cmdline:
        Improper = false;
        break;
      case DefClass::Undefined:
        // Never defined in any TU: a configure-time feature that is
        // disabled in this build. A definition seen in some other TU
        // (header guards, conditional compat defines) clears it.
        Improper = !SourceDefs.count(S.Name) && !CmdlineDefs.count(S.Name);
        break;
      }
    }

    if (!Improper)
      continue;

    llvm::StringRef Rel = stripLustrePrefix(S.Filename);
    llvm::errs() << S.Name << " " << Rel << ":" << S.Line << " FEATURE "
                 << fileKind(Rel) << "\n";
  }
}

} // namespace

std::unique_ptr<Lint> makeFeatureMacrosLint() {
  return std::make_unique<FeatureMacrosLint>();
}
