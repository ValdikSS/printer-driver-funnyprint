SHELL = /bin/sh
PREFIX ?= /usr
DESTDIR ?=
LIBDIR ?= $(PREFIX)/lib
BINDIR ?= $(PREFIX)/bin
ALTBINDIR ?= $(PREFIX)/local/bin
DATAROOTDIR ?= $(PREFIX)/share

.PHONY: install

all:    ppd test-ppd dist

ppd:
	LC_NUMERIC=C ppdc xiqi.drv

test-ppd:
	LC_NUMERIC=C ppdc -t xiqi.drv

clean-python:
	rm -rf build dist src/rastertofunnyprint.egg-info

clean-ppd:
	rm -rf ppd

clean:	clean-python clean-ppd

dist:
	python3 -m build

install:
ifeq ($(DESTDIR),)
	pip3 install .
else
	pip3 install --root="$(DESTDIR)" .
endif
	[ -f $(BINDIR)/rastertofunnyprint ] && ln -s $(BINDIR)/rastertofunnyprint $(DESTDIR)$(LIBDIR)/cups/backend/funnyprint || true
	[ -f $(ALTBINDIR)/rastertofunnyprint ] && ln -s $(ALTBINDIR)/rastertofunnyprint $(DESTDIR)$(LIBDIR)/cups/backend/funnyprint || true
	install -m644 xiqi.drv $(DESTDIR)$(DATAROOTDIR)/cups/drv/funnyprint-xiqi.drv

uninstall:
	pip3 uninstall rastertofunnyprint || true
	rm $(DESTDIR)$(LIBDIR)/cups/backend/funnyprint || true
	rm $(DESTDIR)$(DATAROOTDIR)/cups/drv/funnyprint-xiqi.drv || true
