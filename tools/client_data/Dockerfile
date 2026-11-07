# Build-time network is only for the pinned source repositories and NuGet.
# Runtime extraction is offline and mounts the game installation read-only.
FROM mcr.microsoft.com/dotnet/sdk@sha256:747cbade5bfd918a175ac32f8f1d931edec2c7d0d5397a3de72eaef1ac1f66d2 AS build
ARG TACTSHARP_SHA
ARG DBCD_SHA
ARG WOWDBDEFS_SHA
WORKDIR /src
RUN for r in TACTSharp:$TACTSHARP_SHA DBCD:$DBCD_SHA WoWDBDefs:$WOWDBDEFS_SHA; do \
      n=${r%%:*}; s=${r##*:}; test -n "$s"; \
      git init -q $n && git -C $n fetch -q --depth 1 https://github.com/wowdev/$n.git $s && git -C $n checkout -q FETCH_HEAD; \
    done
COPY app/ClientData.csproj app/Program.cs app/fdids.csv /src/app/
RUN dotnet publish /src/app -c Release -o /out \
    && cp /src/app/fdids.csv /out/ \
    && cp -r /src/WoWDBDefs/definitions /out/definitions

FROM mcr.microsoft.com/dotnet/runtime@sha256:0cf09aaccf6a9f68900aed4c73fb28fe25bb59ec85de4dea08ba02240a8635f3
COPY --from=build /out /app
WORKDIR /work
ENTRYPOINT ["dotnet", "/app/ClientData.dll", "--dbd", "/app/definitions", "--fdids", "/app/fdids.csv"]
