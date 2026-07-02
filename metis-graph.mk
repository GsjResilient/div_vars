# Make helpers for KarypisLab/METIS, the graph partitioning tool.
#
# What this METIS does:
#   METIS partitions graphs, partitions finite-element meshes, and computes
#   fill-reducing orderings for sparse matrices. For AIG workflows, it can be
#   used after you convert an AIG/AIGER circuit into an undirected graph in
#   METIS graph format.
#
# Why use it:
#   Use METIS when you want balanced subgraphs while minimizing cut edges.
#   That is useful for circuit decomposition, distributed simulation,
#   placement-style preprocessing, sparse linear algebra, and parallel workloads
#   where cross-partition communication is expensive.
#
# How to use this file:
#   make -f metis-graph.mk help
#   make -f metis-graph.mk build
#   make -f metis-graph.mk install
#   make -f metis-graph.mk partition GRAPH=path/to/graph.metis PARTS=4
#
# Notes for AIG files:
#   METIS does not read .aig/.aag directly. Convert the AIG into a METIS-format
#   undirected graph first, then run the partition target below.

SHELL := /bin/sh

ROOT_DIR ?= $(CURDIR)
PREFIX ?= $(ROOT_DIR)/.local/metis

METIS_DIR ?= $(ROOT_DIR)/METIS
GKLIB_PATH ?= $(PREFIX)

CC ?= gcc
SHARED ?= 1
PARTS ?= 2
GRAPH ?=
AIG ?= $(ROOT_DIR)/test_15_TOP32_72.aig
OUT_DIR ?= $(ROOT_DIR)/aig_partition_out

GPMETIS ?= $(PREFIX)/bin/gpmetis
NDMETIS ?= $(PREFIX)/bin/ndmetis
MPMETIS ?= $(PREFIX)/bin/mpmetis
PYTHON ?= python3

.DEFAULT_GOAL := help

.PHONY: help info check-metis build install uninstall partition order mesh-partition aig-cut check-graph check-aig clean distclean

help:
	@printf '%s\n' 'KarypisLab/METIS targets:'
	@printf '%s\n' '  info                         Explain what METIS does and why to use it.'
	@printf '%s\n' '  build                        Configure and build local METIS.'
	@printf '%s\n' '  install                      Install METIS binaries/libs under PREFIX.'
	@printf '%s\n' '  partition GRAPH=file PARTS=N  Run gpmetis graph partitioning.'
	@printf '%s\n' '  aig-cut AIG=file PARTS=2       Partition an AIG and print cut variables.'
	@printf '%s\n' '  order GRAPH=file              Run ndmetis fill-reducing ordering.'
	@printf '%s\n' '  mesh-partition GRAPH=file PARTS=N'
	@printf '%s\n' '                               Run mpmetis mesh partitioning.'
	@printf '%s\n' '  clean                        Clean the local METIS build.'
	@printf '%s\n' '  distclean                    Remove METIS build dir and local install prefix.'
	@printf '%s\n' ''
	@printf '%s\n' 'Variables: METIS_DIR=./METIS GKLIB_PATH=.local/metis PREFIX=.local/metis'
	@printf '%s\n' '           CC=gcc SHARED=1 GRAPH=graph.metis AIG=test_15_TOP32_72.aig PARTS=4'

info:
	@printf '%s\n' 'What it is'
	@printf '%s\n' '  METIS is a C toolkit and command-line suite for serial graph partitioning,'
	@printf '%s\n' '  finite-element mesh partitioning, and sparse-matrix ordering.'
	@printf '%s\n' ''
	@printf '%s\n' 'How to use'
	@printf '%s\n' '  1. Install GKlib, then set GKLIB_PATH to that install prefix if needed.'
	@printf '%s\n' '  2. make -f metis-graph.mk install'
	@printf '%s\n' '  3. Convert your input, such as .aig/.aag, into METIS graph format.'
	@printf '%s\n' '  4. make -f metis-graph.mk partition GRAPH=graph.metis PARTS=4'
	@printf '%s\n' '  For AIG cut variables: make -f metis-graph.mk aig-cut PARTS=2'
	@printf '%s\n' ''
	@printf '%s\n' 'Why use it'
	@printf '%s\n' '  It finds balanced graph partitions with small edge cuts, which reduces'
	@printf '%s\n' '  cross-partition coupling and communication.'

check-metis:
	@test -f "$(METIS_DIR)/programs/cmdline_gpmetis.c" || { \
		printf 'METIS source not found under METIS_DIR=%s\n' "$(METIS_DIR)"; \
		exit 1; \
	}

build: check-metis
	cd "$(METIS_DIR)" && make config cc="$(CC)" shared="$(SHARED)" prefix="$(PREFIX)" gklib_path="$(GKLIB_PATH)" && make

install: build
	cd "$(METIS_DIR)" && make install

uninstall:
	test ! -d "$(METIS_DIR)" || cd "$(METIS_DIR)" && make uninstall

check-graph:
	@test -n "$(GRAPH)" || { printf '%s\n' 'Set GRAPH=path/to/graph.metis'; exit 1; }
	@test -f "$(GRAPH)" || { printf 'GRAPH not found: %s\n' "$(GRAPH)"; exit 1; }

check-aig:
	@test -n "$(AIG)" || { printf '%s\n' 'Set AIG=path/to/file.aig'; exit 1; }
	@test -f "$(AIG)" || { printf 'AIG not found: %s\n' "$(AIG)"; exit 1; }

partition: check-graph
	"$(GPMETIS)" "$(GRAPH)" "$(PARTS)"
	@printf 'Partition file: %s.part.%s\n' "$(GRAPH)" "$(PARTS)"

aig-cut: check-aig
	$(PYTHON) "$(ROOT_DIR)/scripts/partition_aig_cutpoints.py" "$(AIG)" --parts "$(PARTS)" --out-dir "$(OUT_DIR)"

order: check-graph
	"$(NDMETIS)" "$(GRAPH)"

mesh-partition: check-graph
	"$(MPMETIS)" "$(GRAPH)" "$(PARTS)"
	@printf 'Partition file: %s.epart.%s or %s.npart.%s\n' "$(GRAPH)" "$(PARTS)" "$(GRAPH)" "$(PARTS)"

clean:
	test ! -d "$(METIS_DIR)" || cd "$(METIS_DIR)" && make clean

distclean:
	test ! -d "$(METIS_DIR)" || cd "$(METIS_DIR)" && make distclean
	rm -rf "$(PREFIX)"
