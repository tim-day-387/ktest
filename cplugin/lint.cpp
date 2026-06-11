// SPDX-License-Identifier: Apache-2.0
//
// Lint registry and shared report helpers.

#include "lint.h"

#include "clang/Basic/SourceManager.h"
#include "clang/Index/USRGeneration.h"
#include "llvm/Support/Path.h"

std::vector<std::unique_ptr<Lint>> makeAllLints() {
  std::vector<std::unique_ptr<Lint>> Lints;
  Lints.push_back(makeUnusedFunctionsLint());
  Lints.push_back(makeUnusedMacrosLint());
  Lints.push_back(makeGlobalHeadersLint());
  Lints.push_back(makeFeatureMacrosLint());
  return Lints;
}

static bool endsWith(llvm::StringRef S, llvm::StringRef Suffix) {
  return S.size() >= Suffix.size() &&
         S.substr(S.size() - Suffix.size()) == Suffix;
}

bool inLustreTree(llvm::StringRef Path) {
  return Path.find("lustre-release") != llvm::StringRef::npos;
}

llvm::StringRef stripLustrePrefix(llvm::StringRef Path) {
  size_t Pos = Path.find("lustre-release/");
  if (Pos != llvm::StringRef::npos)
    return Path.substr(Pos + sizeof("lustre-release/") - 1);
  return Path;
}

llvm::StringRef fileKind(llvm::StringRef Rel) {
  return (endsWith(Rel, ".h") || endsWith(Rel, ".hpp") ||
          endsWith(Rel, ".hxx") || endsWith(Rel, ".hh") ||
          endsWith(Rel, ".H"))
             ? "HEADER"
             : "BODY";
}

bool getUSRForDecl(const clang::Decl *Decl, std::string &USR) {
  llvm::SmallVector<char, 128> Buff;

  if (clang::index::generateUSRForDecl(Decl, Buff))
    return false;

  USR = std::string(Buff.data(), Buff.size());
  return true;
}

// Canonicalize a path for use as a cross-TU map key. The same header may be
// reached via different relative paths in different TUs (e.g.
// "include/uapi/../../ldlm/foo.h" vs "ldlm/foo.h"); without normalization
// those produce distinct keys and use-counts never merge.
std::string canonicalizePath(const clang::SourceManager &SM, clang::FileID FID,
                             llvm::StringRef Fallback) {
  if (auto FE = SM.getFileEntryRefForID(FID)) {
    llvm::StringRef Real = FE->getFileEntry().tryGetRealPathName();
    if (!Real.empty())
      return Real.str();
  }
  llvm::SmallString<256> Buf(Fallback);
  SM.getFileManager().makeAbsolutePath(Buf);
  llvm::sys::path::remove_dots(Buf, /*remove_dot_dot=*/true);
  return std::string(Buf);
}
