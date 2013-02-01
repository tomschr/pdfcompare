#! /usr/bin/python
#
# pdf_highlight.py -- command line tool to show search or compare results in a PDF
#
# (c) 2012-2013 Juergen Weigert jw@suse.de
# Distribute under GPL-2.0 or ask
#
# 2012-03-16, V0.1 jw - initial draught: argparse, pdftohtml-xml, font.metrics
# 2012-03-20, V0.2 jw - all is there, but the coordinate systems of my overlay 
#                       does not match. Sigh.
# 2013-01-12, V0.3 jw - support encrypted files added,
#                       often unencrypted is actually encrypted with key=''
#                     - coordinate transformation from xml to pdf canvas added
#                     - refactored: xml2wordlist, xml2fontinfo, create_mark
#                     - added experimental zap_letter_spacing()
# 2013-01-13, V0.4 jw - added class DecoratedWord
#                     - option --compare works!
# 2013-01-14, V0.5 jw - added xmlfile2wordlist, textfile2wordlist. fixed -e
#                     - added option --mark A,D,C
# 2013-01-15, V0.6 jw - added anno_popup() and friends. Horrible hack.
#                     - Added option --no-anno
#                     - With -c: added line counting to xml input
#                       top/center/bottom indicator for pdf.
# 2013-01-16, V0.7 jw - minor bugfixing.
#                     - Added Changemarks by: page_watermark().
#                     - /Creator /Producer /ModDate writing.
# 2013-01-17, V0.8 jw - added opcodes_post_proc() to make replace operations 
#                       more the human friendly.
#                     - calling compressContentStreams() unless --no-compression
#                     - higher --transparency is now more transparent, not less.
#                     - delete markers at end of previous text, not start of next.
# 2013-01-18, V0.9 jw - hit statistics +-~= added
#                     - page break indicators in catwords() added
#                     - new option -F -first-page added.
#                     - line wrapping for okular popups, if over 60 chars.
#                     - added page break markers in annotations.
#                     - smaller files: we merge once, and help ourselves with /Annots.
# 2013-01-21, V1.0 jw - added option --features popup(aka anno),highight,changebar
#                     - removed option --no-anno in favour of option --features.
#                     - improved option --search-colors to handle all 4 colors.
#                     - fixed features default. 
#                     - made -c optional.
#                     - added -n --no-output. Return value compatible with /bin/cmp.
# 2013-01-23, V1.1 jw - backwards and forwards navigation code added for easily
#                       finding next and previous page with changes. Using 
#                       page_ref_magic as a placeholder with dummy self refs.
#                     - implemented proper relocation and merge in mergeAnnots().
#                     - some off-by-one errors fixed with first_page, last_page.
#                     - some python3 porting: print(), key in dict, isinstance()
# 2013-01-24, V1.1a jw - new --feature margin; ignore a certain margin on the 
#                        pages option -M  added. Unfinished.
# 2013-01-28, V1.1b jw - no more subtracting from NoneType
#                      - bugfix in mergeAnnots()
#                      - factored out highlight_height and reduced from 
#                        1.4 to 1.2 for less overlap.
# 2013-01-29, V1.2  jw - added bbox_overlap(), bbox_inside() in_bbox_interpolated()
#                        to implement margin feature.
# 2013-01-31, V1.3  jw - added hyphenation merge inside opcodes_post_proc().
#                      - added option strict, to suppress zap_letter_spacing() and 
#                        to prevent hyphenation merge.
#                      - removed needless parameter tag from markword()
#                      - finished an implementation of --spell using hunspell in pipe()  
#
# osc in devel:languages:python python-pypdf >= 1.13+20130112
#  need fix from https://bugs.launchpad.net/pypdf/+bug/242756
# osc in devel:languages:python python-reportlab
# osc in devel:languages:python python-pygame
# osc in X11:common:Factory poppler-tools 
#
# needs module difflib from python-base
#
# Feature request:
# - poppler-tools:/usr/bin/pdftohtml -xml should report a rotation angle, 
#   if text is not left-to-right. And it should report spacing adjustements 
#   within a string.
#
# TODOs: see extra file TODO.md 

# Compatibility for older Python versions
from __future__ import with_statement

__VERSION__ = '1.3'

from cStringIO import StringIO
from pyPdf import PdfFileWriter, PdfFileReader, generic as Pdf
from reportlab.pdfgen import canvas
from reportlab.lib.colors import Color

import re, time
from pprint import pprint
import xml.etree.cElementTree as ET
import sys, os, subprocess
from argparse import ArgumentParser
import pygame.font as PGF
from difflib import SequenceMatcher

# I fail to understand the standard encode() decode() methods.
# But the codecs module always does what I mean.
import codecs

# allow debug printing into less:
sys.stdout = codecs.getwriter('utf8')(sys.stdout)
debug = False
page_ref_magic = "675849302 to page "     # a token we use to patch the page objects.
page_ref_plain = "to page "     # this will be visible as a popup on navigation marks.

highlight_height = 1.2  # some fonts cause too much overlap with 1.4
                        # 1.2 is often not enough to look symmetric.

# from pdfminer.fontmetrics import FONT_METRICS
# FONT_METRICS['Helvetica'][1]['W']
#  944

def mergeAnnotsRelocate(dest_p, src_p, first_page=0):
  ### Links to Pages look like this:
  # <<
  # /Contents (to page 1)
  # /Dest [ 14 0 R /Fit ]
  # /Type /Annot
  # /Rect [ 571.46880 0 595.28000 11.90560 ]
  # /Border [ 0 0 0 ]
  # /Subtype /Link
  # >> 
  ### Whereas URL links have an /A object but no /Contents or /Dest.
  #
  ### 
  # This is a hack:
  # while writing navigation marks, we only have one page canvas at a time.
  # Therefore we create self references there, and get 
  # "/Dest": [IndirectObject(33,0), "/Fit"]
  # entries where the IndirectObject() points to the src_p itself.
  # We replace this IndirectObject() with the one of the referenced page 
  # from dest_p's stream.

  # First we fetch the list of all IndirectObject()s for all the pages 
  # in the dest stream.
  if "/Parent" in dest_p:
    pages_a = dest_p["/Parent"].getObject().get("/Kids", [])
  else:
    pages_a = []
    print("mergeAnnots Warning: no /Parent in dest_p")

  if "/Annots" in src_p:
    annots = src_p["/Annots"] 
    for a in annots:
      o = a.getObject() # a is an IndirectObject()
      if "/Contents" in o:
        if debug > 1: pprint(["mergeAnnots old:", o])
        m = re.match(page_ref_magic+"(\d+)$", o["/Contents"])
        if m:
          p_nr = m.group(1)
          o[Pdf.NameObject("/Contents")] = Pdf.createStringObject(page_ref_plain+p_nr)
          if len(pages_a) > int(p_nr)+first_page:
            o["/Dest"][0] = pages_a[int(p_nr)+first_page]
            if debug > 1: pprint(["mergeAnnots new:", o])
          else:
            print("mergeAnnots failed: page_idx %d out of range. Have %d" % 
              (int(p_nr)+first_page, len(pages_a)))
        else:
          print("mergeAnnots failed: page_ref_magic not found: '%s'" % o["/Contents"])
    if "/Annots" in dest_p:
      if debug:
        print("mergeAnnots: append %d+%d" % (len(dest_p["/Annots"]), len(annots)))
      dest_p["/Annots"].extend(annots)
    else:
      dest_p[Pdf.NameObject("/Annots")] = annots


def page_changemarks(canvas, mediabox, marks, page_idx, trans=0.5, cb_x=0.98,cb_w=0.007, min_w=0.01, ext_w=0.05, features='C,H,A,N'):
  # cb_x=0.98 changebar near right edge
  # cb_x=0.02 changebar near left edge
  # min_w=0.05: each mark is min 5% of the page width wide. If not we add extenders.

  anno=False
  highlight=False
  changebar=False
  navigation=False
  features = map(lambda x: x[0].upper(), features.split(','))
  if 'A' in features: anno=True
  if 'P' in features: anno=True         # aka popup
  if 'H' in features: highlight=True
  if 'C' in features: changebar=True
  if 'N' in features: navigation=True

  # mediabox [0, 0, 612, 792], list of 4x float or FloatObject
  # FloatObject does not support arithmetics with float. Needs casting. Sigh.
  # marks = { h:1188, w:918, x:0, y:0, rect: [{x,y,w,h,t},...], nr:1, nav_fwd:9, nav_bwd:4 }
  def x2c(x):
    return (0.0+x*float(mediabox[2])/marks['w'])
  def w2c(w):
    return (0.0+w*float(mediabox[2])/marks['w'])
  def y2c(y):
    return (0.0+float(mediabox[3])-y*float(mediabox[3])/marks['h'])
  def h2c(h):
    return (0.0+h*float(mediabox[3])/marks['h'])

  def nav_mark_fwd(canv, target_page, radius=5):
    w,h = canv._pagesize
    w=float(w)
    h=float(h)
    r = w * radius * 0.01       # percent of page width
    canv.wedge(w-r-r,-r, w,r,     45,90, fill=1, stroke=0)     # bottom
    dest = "jump_"+str(canv.getPageNumber())
    canv.linkAbsolute(page_ref_magic+str(target_page), dest, (w-r-r,0, w,r))

  def nav_mark_bwd(canv, target_page, radius=5):
    w,h = canv._pagesize
    w=float(w)
    h=float(h)
    r = w * radius * 0.01       # percent of page width
    canv.wedge(w-r-r,h-r, w,h+r, 225,90, fill=1, stroke=0)     # top
    dest = "jump_"+str(canv.getPageNumber())
    canv.linkAbsolute(page_ref_magic+str(target_page), dest, (w-r-r,h, w,h-r))

  def anno_popup(canv, x,y, w,h, mark):
    # We misuse linkURL() as this is the only annotation, that a) can be written with reportlab() and b)
    # works both in acroread and ocular. HACK: For acroread, we include .: at the beginning, this prevents
    # file://full_path expansion on strings that do not look like urls.
    text = mark.get('t', '.') + ':'
    if 'o' in mark:
      if isinstance(mark['o'], list):
        text += mark['o'][1]+': '+ mark['o'][0]
      else:
        text += ' '+mark['o']
    # need ascii here. anything else triggers
    # UnicodeDecodeError: 'utf16' codec can't decode bytes in position 5484-5485: illegal UTF-16 surrogate
    # from File "/usr/lib/python2.7/site-packages/pyPdf/generic.py", line 248, in createStringObject
    try:
      # this can fail with: 'ascii' codec can't decode byte 0xe2
      text = text.encode('ascii', errors='replace')
    except:
      text = "text.encode('ascii', errors='ignore')"
    canv.linkURL(text, (x, y, x+w, y+h), relative=0) # , Border="[ 1 1 1 ]")
  
  cb_x = (cb_x-0.5*cb_w) * marks['w']     # relative to pdf page width 
  cb_w = cb_w            * marks['w']     # relative to pdf page width 
  min_w = min_w          * float(mediabox[2])    # relative to xml page width 
  ext_w = ext_w          * float(mediabox[2])    # extenders, if needed

  if navigation:
    canvas.setFillColor(Color(marks['nav_c'][0],marks['nav_c'][1],marks['nav_c'][2], alpha=trans))
    # dummy bookmarks for self references, to be relocated by mergeAnnots()
    canvas.bookmarkPage("jump_%s" % canvas.getPageNumber())
    if 'nav_bwd' in marks: nav_mark_bwd(canvas, marks['nav_bwd'], 2)
    if 'nav_fwd' in marks: nav_mark_fwd(canvas, marks['nav_fwd'], 2)

  canvas.setFont('Helvetica',5)
  ### a testing grid
  if debug > 1:
    for x in range(0,13):
      for y in range(0,50):
        canvas.drawString(50*x,20*y,'.(%d,%d)' % (50*x,20*y))
  if debug: canvas.setFont('Helvetica',16)
  for m in marks['rect']:
    canvas.setFillColor(Color(m['c'][0],m['c'][1],m['c'][2], alpha=trans))
    canvas.setStrokeColor(Color(m['c'][0],m['c'][1],m['c'][2], alpha=0.5*trans))
    # m = {'h': 23, 'c': [1,0,1], 't': 'equ', 'w': 76.56716417910448, 'x': 221.0, 'y': 299}
    (x,y,w,h) = (m['x'], m['y'], m['w'], m['h'])
    if w < min_w:
      # normally happens with m['t'] == 'del'
      if debug > 1: print("min_w:%s (%s)" % (min_w, w))
      if highlight:
        # delete marker: two horizontal and one vertical bar.
        canvas.rect(x2c(x-ext_w),y2c(y+0.2*h), w2c(w+2*ext_w),h2c(0.2*h), fill=1, stroke=0)
        canvas.rect(x2c(x-ext_w),y2c(y-(highlight_height-0.2)*h), w2c(w+2*ext_w),h2c(0.2*h), fill=1, stroke=0)
        x = x - (0.5 * (min_w-w))
        canvas.rect(x2c(x),y2c(y),w2c(min_w),h2c(h*(highlight_height-0.2)), fill=1, stroke=0)
      if anno:
        anno_popup(canvas, x2c(x),y2c(y),    w2c(min_w),h2c(h*highlight_height), m)
    else:
      # multiply height h with (highlight_height -- ca 1.2) to add some top 
      # padding, similar to the bottom padding that is automatically added
      # due to descenders extending across the font baseline.
      if highlight:
        if m['t'] == 'spl':
          canvas.rect(x2c(x),y2c(y),    w2c(w),h2c(h*0.2), fill=1, stroke=0) # underline only
        else:
          canvas.rect(x2c(x),y2c(y),    w2c(w),h2c(h*highlight_height), fill=1, stroke=0)
      if anno:
        anno_popup(canvas, x2c(x),y2c(y),    w2c(w),h2c(h*highlight_height), m)

    if changebar:
      canvas.rect(x2c(cb_x),  y2c(y),w2c(cb_w),  h2c(h*highlight_height), fill=1, stroke=1)
      if debug > 1:
        canvas.drawString(x2c(x),y2c(y),'.(%d,%d)%s(%d,%d)' % (x2c(x),y2c(y),m['t'],x,y))
        pprint(m)
        return      # shortcut, only the first word of the page

def page_watermark(canv, box, argv, color=[1,0,1], trans=0.5, p_w=None, p_h=None, margins=None, features='W,M'):
  f_watermark=False
  f_margins=False
  features = map(lambda x: x[0].upper(), features.split(','))
  if 'M' in features: f_margins=True
  if 'W' in features: f_watermark=True

  if f_margins and margins:
    w,h = canv._pagesize
    w=float(w)
    h=float(h)
    m_n=margins['n']*float(box[3])/p_h
    m_e=margins['e']*float(box[2])/p_w
    m_w=margins['w']*float(box[2])/p_w
    m_s=margins['s']*float(box[3])/p_h
    canv.setFillColor(Color(margins['c'][0],margins['c'][1],margins['c'][2], alpha=trans))
    if m_n: canv.rect(0,h-m_n, w,m_n, fill=1, stroke=0)
    if m_s: canv.rect(0,0, w,m_s, fill=1, stroke=0)
    if m_e: canv.rect(w-m_e,m_s, m_e,h-m_n-m_s, fill=1, stroke=0)
    if m_w: canv.rect(0,m_s, m_w,h-m_n-m_s, fill=1, stroke=0)

  if f_watermark:
    canv.setFont('Helvetica',5)
    av = []
    for arg in argv:
      m=re.match("\S\S\S\S+(/.*?)$", arg)
      if m: arg = "..."+m.group(1)
      av.append(arg)
    text = "Highlights added by (V" + __VERSION__ + "):  " + " ".join(av)
    canv.setFillColor(Color(color[0],color[1],color[2], alpha=trans))
    canv.drawString(15,10,text)


def pdf2xml(parser, infile, key=''):
  """ read a pdf file with pdftohtml and parse the resulting xml into a dom tree
      the first parameter, parser is only used for calling exit() with proper messages.
  """
  pdftohtml_cmd = ["pdftohtml", "-q", "-i", "-nodrm", "-nomerge", "-stdout", "-xml"]
  if len(key):
    pdftohtml_cmd += ["-upw", key]
  try:
    (to_child, from_child) = os.popen2(pdftohtml_cmd + [infile])
  except Exception,e:
    parser.exit("pdftohtml -xml failed: " + str(e))

  try:
    dom = ET.parse(from_child)
  except Exception,e:
    parser.exit("pdftohtml -xml failed.\nET.parse: " + str(e) + ")\n\n" + parser.format_usage())
  print("pdf2xml done")
  return dom

class DecoratedWord(list):
  def __eq__(a,b):
    return a[0] == b[0]
  def __hash__(self):
    return hash(self[0])

def xmlfile2wordlist(fname):
  """ works well with xml from pdftohtml -xml.
      """
  wl = []
  elementcount = 0

  #tree= ET.parse(fname)
  #dom = tree.getroot()
  #for elem in dom.iter():

  ## line number counting idea from 
  ## http://bytes.com/topic/python/answers/535191-elementtree-line-numbers-iterparse
  class FileWrapperLineNo:
    def __init__(self, source):
      self.source = source
      self.lineno = 0
    def read(self, bytes):
      s = self.source.readline()
      self.lineno += 1
      return s

  f = FileWrapperLineNo(open(fname))
  for event, elem in ET.iterparse(f, events=("start", "end")):
    if event == "start":
      elementcount += 1
      ## we could grab all from the root element, 
      ## but we want to count elements.
      # t = "".join(elem.itertext())
      if elem.text:
        for w in elem.text.split():
          wl.append(DecoratedWord([w,None,None,{'e':elementcount, 'l':f.lineno}]))
  return wl

def textfile2wordlist(fname):
  """ CAUTION if you create your text files with pdftotxt, 
      things may appear in different ordering than with pdftohtml, resulting
      in an enormous diff.
      """
  wl = []
  # assume .txt files are utf8 encoded, but please survive binary garbage.
  with codecs.open(fname, 'r', 'utf-8', errors='ignore') as f:
    for lnr, line in enumerate(f):
      for w in line.split():
        wl.append(DecoratedWord([w,None,None,{'l':lnr}]))
  return wl

def bbox_inside(bb1, bb2):
  """ checks if bb2 is inside the bounding box bb1.
      The bounding box format is [x1,y1,x2,y2].
      If bb2 is a bounding box, all 4 corners must be inside bb1.
      If bb2 has only length 2, it is interpreted as a point [x,y].
  """

  if len(bb2) < 4:
    # a point was given, promote to 0-size bbox. Same price.
    x,y = bb2
    bb2 = [x,y,x,y]
  if bb2[0] < bb1[0]: return False
  if bb2[1] < bb1[1]: return False
  if bb2[2] > bb1[2]: return False
  if bb2[3] > bb1[3]: return False
  return True

def bbox_overlap(bb1, bb2):
  """ bb1 and bb2 are expected as four element rectangles in the format
      [x1,y1,x2,y2]. Returns True if any corner of bb2 is in bb1 
                               or if any corner of bb1 is in bb2.
      That should be a valid overlap test, no?
  """
  x1,y1,x2,y2 = bb2
  if bbox_inside(bb1, [x1,y1]): return True
  if bbox_inside(bb1, [x1,y2]): return True
  if bbox_inside(bb1, [x2,y2]): return True
  if bbox_inside(bb1, [x2,y1]): return True
  x1,y1,x2,y2 = bb1
  if bbox_inside(bb2, [x1,y1]): return True
  if bbox_inside(bb2, [x1,y2]): return True
  if bbox_inside(bb2, [x2,y2]): return True
  if bbox_inside(bb2, [x2,y1]): return True
  return False

def in_bbox_interpolated(bbox, word):
  """calculate an approximation of the on-page position of word[0].
     We use the known coordinates word[3]['x,y,w,h] of the text word[1], 
     in which word[0] is contained as a substring starting at position word[2].
     A simple constant width character interpolation is done for speed.
     A more excat interpolation is possible using the font metrics word[3]['f']
  """
  if bbox is None: 
    return True  # the undefined bounding box includes all

  # coordinates of word[1]
  x1 = float(word[3]['x'])
  x2 = float(word[3]['w']) + x1
  y1 = float(word[3]['y'])
  y2 = float(word[3]['h']) + y1
  if bbox_inside(bbox, [x1,y1,x2,y2]):
    return True # fast track. All of word[1] is in, so the word[0] is also in.

  if not len(word[1]): return False     # zero-size and not completly in, means out.

  i = word[2]
  l = len(word[0])
  char_width = float(x2-x1)/len(word[1])
  x1 += i * char_width
  x2 = x1 + l * char_width
  # Given the fast track above, maybe for the rest, a
  # correct font metrics interpolation is affordable?
  # The impact is linear with the input text size, beware.
  # Currently impact of metrics calculation is only linar with the 
  # result set size.
  if bbox_overlap(bbox, [x1,y1,x2,y2]):
    return True         
  return False
  
def textline2wordlist(text, context, bbox=None):
  """returns a list of 4-element lists like this:
     [word, text, idx, context]
     where the word was found in the text string at offset idx.
     words are defined as any printable text delimited by whitespace.
     just as str.split() would do.
     Those 4-element lists are cast into DecoratedWord.
     The DecoratedWord type extends the list type, so that it is hashable and
     comparable using only the "word" which is the first element of the four. 
     Thus our wordlists work well as sequences with difflib, although they also
     transport all the context to compute exact page positions later.

     A bbox (x1,y1, x2, y2) with x1 < x2, y1 < y2 can be specified to prefilter
     the wordlist. Only words that are (at least partially) inside the bbox
     will be returned.
  """

  wl = []
  idx = 0
  tl = re.split("(\s+)", text)
  while True:
    if len(tl)==0: break
    head = tl.pop(0)
    if len(head):
      word = [head, text, idx, context]
      if in_bbox_interpolated(bbox, word):
        wl.append(DecoratedWord(word))
    if len(tl)==0: break
    sep = tl.pop(0)
    idx += len(sep)+len(head)
  return wl
  
def xml2wordlist(dom, first_page=None, last_page=None, margins=None):
  """input: a dom tree as generated by pdftohtml -xml.
     first_page, last_page start counting at 0.
     If margins is not None, the coordinates of all words are filtered against 
     a bounding box constructed by reducing the page box.
     output: a wordlist with all the metadata so that the exact coordinates
             of each word can be calculated.
  """
  ## Caution: 
  # <text font="1" height="14" left="230" top="203" width="635">8-bit microcontroller based on the AVR enhanced RISC architecture. By executing powerful</text>
  # <text font="1" height="14" left="230" top="223" width="635">i n s t r u c t i o n s   i n   a   s i n g l e   c l o c k   c y c l e ,   t h e</text>
  ## pdftohtml -xml can return strings where each letter is padded with a whitespace. 
  ## zap_letter_spacing() handles this (somewhat)
  ## Seen in atmega164_324_644_1284_8272S.pdf

  if first_page is None: first_page = 0
  wl=[]
  p_nr = 0
  for p in dom.findall('page'):
    if not last_page is None:
      if p_nr > int(last_page):
        break
    p_nr += 1
    if p_nr <= int(first_page):
      continue
    p_h = float(p.attrib['height'])
    p_w = float(p.attrib['width'])

    # default bounding box is entire page:
    # bbox ordering is x1,y1, x2, y2 where x1,y1 are the smaller values.
    p_bbox = (0, 0, p_w, p_h)
    if margins is not None:
      p_bbox = (margins['n'], margins['w'], p_w - margins['e'], p_h - margins['s'])

    for e in p.findall('text'):
      # <text font="0" height="19" left="54" top="107" width="87"><b>Features</b></text>
      x=e.attrib['left']
      y=e.attrib['top']
      w=e.attrib['width']
      h=e.attrib['height']
      f=e.attrib['font']
      text = ''
      for t in e.itertext(): text += t

      ## crude top,center,bottom location
      if   float(y) > 0.66*p_h: l = 'b'
      elif float(y) > 0.33*p_h: l = 'c'
      else:                     l = 't'
      wl += textline2wordlist(text, {'p':p_nr, 'l':l, 'x':x, 'y':y, 'w':w, 'h':h, 'f':f}, p_bbox)
    #pprint(wl)
  print("xml2wordlist: %d pages" % (p_nr-int(first_page)))
  return wl

def xml2fontinfo(dom, last_page=None):
  # last_page starts counting at 0 and is inclusive.
  finfo = [None]      # each page may add (or overwrite?) some fonts
  p_finfo = {}
  p_nr = 0
  for p in dom.findall('page'):
    if not last_page is None:
      if p_nr > int(last_page):
        break
    p_nr += 1
    p_finfo = p_finfo.copy()
    # print("----------------- page %s -----------------" % p.attrib['number'])

    for fspec in p.findall('fontspec'):
      fname = fspec.attrib.get('family', 'Helvetica')
      fsize = fspec.attrib.get('size', 12)
      f_id  = fspec.attrib.get('id')
      f_file = PGF.match_font(fname)
      ######
      # On openSUSE 12.1 Beta 1 (i586,fossy) the call to PGF.Font() triggers this warning:
      # /usr/lib/python2.7/site-packages/pygame/pkgdata.py:27: UserWarning:
      # Module argparse was already imported from
      # /usr/lib/python2.7/argparse.pyc, but /usr/lib/python2.7/site-packages
      # is being added
      f = PGF.Font(f_file, int(0.5+float(fsize)))
      p_finfo[f_id] = { 'name': fname, 'size':fsize, 'file': f_file, 'font':f }
    #pprint(p_finfo)
    finfo.append(p_finfo)
  return finfo

def main():
  parser = ArgumentParser(epilog="version: "+__VERSION__, description="highlight words in a PDF file.")
  parser.def_trans = 0.5
  parser.def_decrypt_key = ''
  parser.def_colors = { 'E': [1,0,1,    'pink'], 
                        'A': [.3,1,.3,  'green'],
                        'D': [1,.3,.3,  'red'],
                        'C': [.9,.8,0,  'yellow'],
                        'M': [.9,.9,.9, 'gray'] }
  parser.def_output = 'output.pdf'
  parser.def_marks = 'A,D,C'
  parser.def_features = 'H,C,P,N,W,M'
  parser.def_margins = '0,0,0,0'
  parser.add_argument("-o", "--output", metavar="OUTFILE", default=parser.def_output,
                      help="write output to FILE; default: "+parser.def_output)
  parser.add_argument("-s", "--search", metavar="WORD_REGEXP", 
                      help="highlight WORD_REGEXP")
  parser.add_argument("-d", "--decrypt-key", metavar="DECRYPT_KEY", default=parser.def_decrypt_key,
                      help="open an encrypted PDF; default: KEY='"+parser.def_decrypt_key+"'")
  parser.add_argument("-c", "--compare-text", metavar="OLDFILE",
                      help="mark added, deleted and replaced text (or see -m) with regard to OLDFILE. \
                            File formats .pdf, .xml, .txt are recognized by their suffix. \
                            The comparison works word by word.")
  parser.add_argument("-m", "--mark", metavar="OPS", default=parser.def_marks,
                      help="specify what to mark. Used with -c. Allowed values are 'add','delete','change','equal'. \
                            Multiple values can be listed comma-seperated; abbreviations are allowed.\
                            Default: " + str(parser.def_marks))
  parser.add_argument("-M", "--margins", metavar="N,E,W,S", default=parser.def_margins,
                      help="specify margin space to ignore on each page. A margin width is expressed \
                      in units of ca. 100dpi. Specify four numbers in the order north,east,west,south. Default: "\
                      + str(parser.def_margins))
  parser.add_argument("-f", "--features", metavar="FEATURES", default=parser.def_features,
                      help="specify how to mark. Allowed values are 'highlight', 'changebar', 'popup', \
                      'navigation', 'watermark', 'margin'. Default: " + str(parser.def_features))
  parser.add_argument("-e", "--exclude-irrelevant-pages", default=False, action="store_true",
                      help="with -s: show only matching pages; with -c: show only changed pages; \
                      default: reproduce all pages from INFILE in OUTFILE")
  parser.add_argument("-n", "--no-output", default=False, action="store_true",
                      help="do not write an output file; print diagnostics only; default: write output file as per -o")
  parser.add_argument("-i", "--nocase", default=False, action="store_true",
                      help="make -s case insensitive; default: case sensitive")
  parser.add_argument("--strict", default=False, action="store_true",
                      help="show really all differences; default: ignore removed hyphenation; ignore character spacing inside a word")
  parser.add_argument("--spell", "--spell-check", default=False, action="store_true",
                      help="run the text body of the (new) pdf through hunspell. Unknown words are underlined.")
  parser.add_argument("-L", "--last-page", metavar="LAST_PAGE",
                      help="limit pages processed; this counts pages, it does not use document \
                      page numbers; see also -F; default: all pages")
  parser.add_argument("-F", "--first-page", metavar="FIRST_PAGE",
                      help="skip some pages at start of document; see also -L; default: all pages")
  parser.add_argument("-t", "--transparency", type=float, default=parser.def_trans, metavar="TRANSP", 
                      help="set transparency of the highlight; invisible: 0.0; full opaque: 1.0; \
                      default: " + str(parser.def_trans))
  parser.add_argument("-D", "--debug", default=False, action="store_true",
                      help="enable debugging. Prints more on stdout, dumps several *.xml or *.pdf files.")
  parser.add_argument("-V", "--version", default=False, action="store_true",
                      help="print the version number and exit")
  parser.add_argument("-X", "--no-compression", default=False, action="store_true",
                      help="write uncompressed PDF. Default: FlateEncode filter compression.")
  parser.add_argument("-C", "--search-color", metavar="NAME=R,G,B", action="append",
                      help="set colors of the search highlights as an RGB triplet; R,G,B ranges are 0.0-1.0 each; valid names are 'add,'delete','change','equal','margin','all'; default name is 'equal', which is also used for -s; default colors are " + 
                      " ".join(map(lambda (x,y): "%s=%s,%s,%s /*%s*/ " %(x,y[0],y[1],y[2],y[3]), 
                      parser.def_colors.items())))
  parser.add_argument("infile", metavar="INFILE", help="the input file")
  parser.add_argument("infile2", metavar="INFILE2", nargs="?", help="optional 'newer' input file; alternate syntax to -c")
  args = parser.parse_args()      # --help is automatic

  args.transparency = 1 - args.transparency     # it is needed reversed.

  if args.version: parser.exit(__VERSION__)
  global debug 
  debug = args.debug

  args.search_colors = parser.def_colors.copy()
  if args.search_color:
    for col in args.search_color:
      val=None
      b=None
      try: (name,val) = col.split('=')
      except: pass 
      if val is None: (name,val)=('equal', col)
      try: (r,g,b) = val.split(',')
      except: pass 
      if b is None: parser.exit("--search-color NAME=R,G,B: no two ',' found in '%s=%s'" % (name,val))
      if name.upper() == 'ALL':
        for c in args.search_colors.keys():
          args.search_colors[c] = [float(r),float(g),float(b)]
      else:
        name = name[0].upper()
        args.search_colors[name] = [float(r),float(g),float(b)]

  margins = parse_margins(args.margins, args.search_colors['M'])

  ## TEST this, fix or disable: they should work well together:
  # if args.search and args.compare_text:
  #   parser.exit("Usage error: -s search and -c compare are mutually exclusive, try --help")

  if args.compare_text is None and args.infile2 is not None:
    args.compare_text,args.infile = args.infile,args.infile2

  if args.search is None and args.compare_text is None and args.spell is None:
    parser.exit("Oops. Nothing to do. Specify either -s or --spell or -c or two input files")

  if not os.access(args.infile, os.R_OK):
    parser.exit("Cannot read input file: %s" % args.infile)
  dom1 = pdf2xml(parser, args.infile, args.decrypt_key)
  dom2 = None
  wordlist2 = None
  if args.compare_text:
    if re.search('\.pdf$', args.compare_text, re.I):
      dom2 = pdf2xml(parser, args.compare_text, args.decrypt_key)
      first_page = args.first_page
      if first_page is not None: first_page = int(first_page) - 1
      last_page = args.last_page
      if last_page is not None: last_page = int(last_page) - 1
      wordlist2 = xml2wordlist(dom2, first_page, last_page, margins=margins)
    elif re.search('\.xml$', args.compare_text, re.I):
      wordlist2 = xmlfile2wordlist(args.compare_text)
    else:
      # assuming a plain text document
      wordlist2 = textfile2wordlist(args.compare_text)

  if debug:
    dom1.write(args.output + ".1.xml")
    if dom2:
      dom2.write(args.output + ".2.xml")

  PGF.init()
  # This pygame.font module is used to calculate widths of all glyphs
  # for words we need to mark. With this calculation, we can determine 
  # the exact position and length of the marks, if the marked word is 
  # only a substring (which it often is).
  # For complete strings, we get the exact positions and size from pdftohtml -xml.
  # Strings returned by pdftohtml are combinations of multiple PDF text fragments.
  # This is good, as pdftohtml reassembles words and often complete lines in a perfectly 
  # readable way. 
  # The downside of this is, that the width and position calculation may be
  # a bit off, due to uneven word-spacing or letter-spacing in the original PDF text line.
  ####
  # f = PGF.Font(PGF.match_font('Times'), 13))
  # f.metrics("Bernoulli") 
  #  [(0, 8, 0, 9, 9), (0, 7, 0, 6, 6), (-1, 5, 0, 6, 4), (-1, 6, 0, 6, 6), (0, 7, 0, 6, 7), (0, 6, 0, 6, 6), (-1, 3, 0, 9, 3), (-1, 3, 0, 9, 3), (-1, 3, 0, 9, 3)]
  # (minx, maxx, miny, maxy, advance)

  input1 = PdfFileReader(file(args.infile, "rb"))
  if input1.getIsEncrypted():
    if input1.decrypt(args.decrypt_key):
      if len(args.decrypt_key):
        print("Decrypted using key='%s'." % args.decrypt_key)
    else:
      parser.exit("decrypt(key='%s') failed." % args.decrypt_key)

  # last_page,first_page start counting at 0,
  # args.last_page, args.first_page start counting at 1.
  last_page = input1.getNumPages()-1
  first_page = 0
  if args.last_page and int(args.last_page) < last_page:
    last_page = int(args.last_page)-1
  if args.first_page:
    first_page = int(args.first_page)-1
    if first_page > last_page:
      first_page = last_page
  print("input pages: %d-%d" % (first_page+1, last_page+1))

  page_marks = pdfhtml_xml_find(dom1, re_pattern=args.search, 
      wordlist=wordlist2,
      nocase=args.nocase,
      first_page=first_page,
      last_page=last_page,
      mark_ops=args.mark,
      margins=margins,
      strict=args.strict,
      spell_check=args.spell,
      ext={'a': {'c':args.search_colors['A']},
           'd': {'c':args.search_colors['D']},
           'c': {'c':args.search_colors['C']},
           'e': {'c':args.search_colors['E']} })

  # pprint(page_marks[0])

  output = PdfFileWriter()
  # Evil hack: there is no sane way to transport DocumentInfo metadata.
  #          : This is the insane way, we duplicate this code from
  #          : PdfFileWriter.__init__()
  # FIXME: We should also copy the XMP metadata from the document.
  try:
    di = input1.getDocumentInfo()

    # update ModDate, Creator, DiffCmd
    selfcmd = " ".join(sys.argv) + ' # V' + __VERSION__ + ' ' + time.ctime()
    if not "/Creator" in di:
      di[Pdf.NameObject('/Creator')] = Pdf.createStringObject(selfcmd)
    elif not '/Producer' in di:
      di[Pdf.NameObject('/Producer')] = Pdf.createStringObject(selfcmd)
    di[Pdf.NameObject('/DiffCmd')] = Pdf.createStringObject(selfcmd)
    di[Pdf.NameObject('/ModDate')] = Pdf.createStringObject(time.strftime("D:%Y%m%d%H%M%S"))
    if debug:
      print("DocumentInfo():")
      pprint(di)
    output._objects.append(di)
  except Exception,e:
    print("WARNING: getDocumentInfo() failed: " + str(e) );

  output._info = Pdf.IndirectObject(len(output._objects), 0, output)

  pages_written = 0
  total_hits = 0

  page_idx = 0
  nav_bwd = None
  for i in range(first_page,last_page+1):
    if not nav_bwd is None: page_marks[i]['nav_bwd'] = nav_bwd
    if len(page_marks[i]['rect']): nav_bwd = page_idx
    page_idx += 1
  nav_fwd = None
  for i in range(last_page,first_page-1,-1):
    page_idx -= 1
    if not nav_fwd is None: page_marks[i]['nav_fwd'] = nav_fwd
    if len(page_marks[i]['rect']): nav_fwd = page_idx

  for i in range(first_page,last_page+1):
    if args.exclude_irrelevant_pages and len(page_marks[i]['rect']) == 0:
      continue
    hitdetails = {'equ':0, 'add':0, 'del':0, 'chg':0, 'spl':0 }
    for r in page_marks[i]['rect']:
      tag = r.get('t','unk')
      if not tag in hitdetails:
        hitdetails[tag] = 0
      hitdetails[tag] += 1
      total_hits += 1
    hits_fmt = ''
    for det,ch in (['add','+'], ['del','-'], ['chg','~'], ['equ','='], ['spl','!']):
      if hitdetails[det]: hits_fmt += '%s%d' % (ch,hitdetails[det])

    print(" page %d: %d hits %s" % (page_marks[i]['nr'], len(page_marks[i]['rect']), hits_fmt))
    # pprint(hitdetails)

    page = input1.getPage(i)
    box = page['/MediaBox']     # landscape look like [0, 0, 794, 595]

    ## create a canvas of correct size, 
    ## paint semi-transparent highlights on the canvas,
    ## then save the canvas to memory string as proper PDF,
    ## merge this string ontop of the original page.
    pdf_str = StringIO()
    c = canvas.Canvas(pdf_str, pagesize=(box[2],box[3]))
    page_watermark(c, box, sys.argv, color=args.search_colors['E'], trans=args.transparency, 
                   p_w=page_marks[i]['w'], p_h=page_marks[i]['h'], margins=margins, features=args.features)
    page_changemarks(c, box, page_marks[i], i-first_page, trans=args.transparency, features=args.features)

    # c.textAnnotation('Here is a Note', Rect=[34,0,0,615], addtopage=1,Author='Test Opacity=0.1',Color=[0.7,0.8,1],Type='/Comment',Opacity=0.1)
    # c.linkURL(".: Here is a Note", (30,10,200,20), relative=0, Border="[ 1 1 1 ]")

    c.save()
    pdf_str.seek(0,0)
    if debug:
      file("canvas_%d.pdf"%i, 'w').write(pdf_str.getvalue())
      pdf_str.seek(0,0)
    input2 = PdfFileReader(pdf_str)
    highlight_page = input2.getPage(0)
    if 0:
      ## We can paint below or above the document.
      ## Below looks better, as the fonts are true black,
      ## but fails completely, if white background is drawn.
      ## Thus the highlight_page must be on top.
      ##
      highlight_page.mergePage(page)
      if not args.no_compression:
        highlight_page.compressContentStreams()
      output.addPage(highlight_page)
    else:
      page.mergePage(highlight_page)
      mergeAnnotsRelocate(page, highlight_page, first_page)
      if not args.no_compression:
        page.compressContentStreams()
      output.addPage(page)

    pages_written += 1

  if args.no_output is False:
    outputStream = file(args.output, "wb")
    output.write(outputStream)
    outputStream.close()
    print("%s (%s pages) written." % (args.output, pages_written))

  if total_hits:
    sys.exit(1)
  else:
    sys.exit(0)


def parse_margins(text,color):
  a = text.split(',')
  if len(a) < 4: a.extend((a[0],a[0],a[0]))
  return {'n':float(a[0]), 'e':float(a[1]), 
          'w':float(a[2]), 's':float(a[3]), 'c':color}


def rendered_text_width(str, font=None):
  """Returns the width of str, in font units.
     If font is not specified, then len(str) is returned.
     """
  if (font is None): return len(str)
  if (len(str) == 0): return 0
  return sum(map(lambda x: x[4], font.metrics(str)))

def rendered_text_pos(string1, char_start, char_count, font=None, xoff=0, width=None):
  """Returns a tuple (xoff2,width2) where substr(string1, ch_start, ch_count) will be rendered
     in relation to string1 being rendered starting at xoff, and being width units wide.

     If font is specified, it is expected to have a metrics() method returning a tuple, where 
     the 5th element is the character width, e.g. a pygame.font.Font().
     Otherwise a monospace font is asumed, where all characters have width 1.

     If width is specified, it is used to recalculate positions so that the entire string1 fits in width.
     Otherwise the values calculated by summing up font metrics by character are used directly.
     """
  pre = string1[:char_start]
  str = string1[char_start:char_start+char_count]
  suf = string1[char_start+char_count:]

  pre_w = rendered_text_width(pre, font)
  str_w = rendered_text_width(str, font)
  suf_w = rendered_text_width(suf, font)
  ratio = 1

  if (width is not None): 
    tot_w = pre_w+str_w+suf_w
    if (tot_w == 0): tot_w = 1
    ratio = float(width)/tot_w
  #pprint([[pre,str,suf,width],[pre_w,str_w,suf_w,tot_w],ratio])
  return (xoff+pre_w*ratio, str_w*ratio)

def create_mark(text,offset,length, font, t_x, t_y, t_w, t_h, ext={}):
  #print("word: at %d is '%s'" % (offset, text[offset:offset+length]))
    
  (xoff,width) = rendered_text_pos(text, offset, length,
                          font, float(t_x), float(t_w))
  #print("  xoff=%.1f, width=%.1f" % (xoff, width))

  mark = {'x':xoff, 'y':float(t_y)+float(t_h),
          'w':width, 'h':float(t_h), 't':text[offset:offset+length]}
  for k in ext:
    mark[k] = ext[k]
  return mark

def zap_letter_spacing(text):
  ###
  # <text font="1" height="14" left="230" top="223" width="635">i n s t r u c t i o n s   i n   a   s i n g l e   c l o c k   c y c l e ,   t h e</text>
  # Does not normally match a word. But xmltohtml -xml returns such approximate renderings for block justified texts.
  # Sigh. One would need to search for c\s*l\s*o\s*c\s*k to find clock there.
  # if every second letter of a string is a whitespace, then remove these extra whitespaces.
  ###
  l = text.split(' ')
  maxw = 0
  for w in l:
    if len(w) > maxw: maxw = len(w)
  if maxw > 1: return text

  # found whitespaces padding as seen in the above example.
  # "f o o   b a r ".split(' ')
  # ['f', 'o', 'o', '', '', 'b', 'a', 'r', '']
  t = ''
  for w in l:
    if len(w) == 0: w = ' '
    t += w
  #print("zap_letter_spacing('%s') -> '%s'" % (text,t))
  return t

def spell_check_word(w):
  if w.lower() in ('files', 'nuernberg', 'ca.'):
    return "dummy implementation. marks the words 'files', 'Nuernberg' and 'ca.'"
  return None

def pdfhtml_xml_find(dom, re_pattern=None, wordlist=None, nocase=False, ext={}, first_page=None, last_page=None, mark_ops="D,A,C", margins=None, strict=False, spell_check=False):
  """traverse the XML dom tree, (which is expected to come from pdf2html -xml)
     find all occurances of re_pattern on all pages, returning rect list for 
     each page, giving the exact coordinates of the bounding box of all 
     occurances. Font metrics are used to interpolate into the line fragments 
     found in the dom tree.
     Keys and values from ext['e'] are merged into the DecoratedWord output for pattern matches and spell check findings.
     If re_pattern is None, then wordlist is used instead. 
     Keys and values from ext['a'], ext['d'], or ext['c'] respectively are merged into 
     the DecoratedWord output for added, deleted, or changed texts (respectivly).
     mark_ops defines which diff operations are marked.
  """

  ######
  def markword(r_dict, wl, idx, attr, fontinfo):
    w = wl[idx]
    p_nr = w[3].get('p','?')
    l = len(w[0])
    off = w[2]
    if attr['t'] == 'del':
      # l=0 special case:
      # very small marker length triggers extenders.
      # decrement idx if idx > 0.
      #  if decrementable (idx > 1), place the marker at the end of the previous word, 
      #  not at the beginning of this word.
      if idx > 0:
         w = wl[idx-1]
         p_nr = w[3].get('p','?')
         off = w[2]+len(w[0])
      l = 0 

    mark = create_mark(w[1], off, l,
          fontinfo[p_nr][w[3]['f']]['font'], 
          w[3]['x'],w[3]['y'],w[3]['w'],w[3]['h'], attr)
    if not p_nr in r_dict: r_dict[p_nr] = []
    r_dict[p_nr].append(mark)

  def catwords(dw, idx1, idx2):
    text = ""
    llen=0
    ypos=None
    p_nr=None
    for w in dw[idx1:idx2]:
      if p_nr is None:
        p_nr = w[3].get('p','')
      if ypos is None:
        ypos = w[3].get('y','')
      if 'p' in w[3] and p_nr != w[3]['p']:
        p_nr = w[3]['p']
        text += " <br> --]page:%d[--" % int(p_nr)
        llen=1000 # fallthrough
      if llen > 100 or ypos != w[3].get('y',''):
        # silly hack for okular. It does not do line wrapping on its own.
        # evince and acroread do it. Okular wraps the line, when I say <br>, 
        # but the others will then print out "<br>". 
        # Please fix okular, someone.
        # These URL annotations are not meant to contain html code. 
        text += " <br> "
        llen = 0
        ypos = w[3].get('y','')
      elif llen > 0:
        text += " "
        llen += 1
      text += w[0]
      llen += len(w[0])
    if 'p' in dw[idx1][3]:
      page_or_elem = 'p'+str(dw[idx1][3]['p'])
    elif 'e' in dw[idx1][3]:
      page_or_elem = 'e'+str(dw[idx1][3]['e'])
    else:
      page_or_elem = '#'
    loc_or_lineno = dw[idx1][3].get('l','')
    if isinstance(loc_or_lineno, int):
      loc_or_lineno = 'l'+str(loc_or_lineno)
    return [text, page_or_elem+loc_or_lineno]
  ######

  fontinfo = xml2fontinfo(dom, last_page)

  ops = {}
  for op in mark_ops.split(','):
    ops[op[0].lower()] = 1

  p_rect_dict = {}   # indexed by page numbers, then lists of marks
  if wordlist or spell_check:
    # generate our wordlist too, so that we can diff against the given wordlist or spellcheck.
    wl_new = xml2wordlist(dom, first_page, last_page, margins=margins)
  if wordlist:
    s = SequenceMatcher(None, wordlist, wl_new, autojunk=False)
    print "SequenceMatcher done"

    def opcodes_post_proc(iter_list):
      ## Often small pieces are replaced by big pieces or vice versa.
      ## For a concept of wordlist we want to replace n words with exactly n words.
      ## Excessive or missing words should be considered an adjacent insert or delete.
      ## get_opcodes() would never do that, as it will always produce one 'equal' beween 
      ## each other operation.
      ##
      ## I now officially love generators in python.
      ##
      for tag, i1, i2, j1, j2 in iter_list:
        if tag == "replace":
          i_len = i2-i1
          j_len = j2-j1
          if not strict and i_len == 2 and j_len == 1:
            if debug:
              print "not strict: check '%s','%s' -> '%s'" % (wordlist[i1][0],wordlist[i1+1][0],wl_new[j1][0])
            # check, if we can optimize it away
            m = re.match("(.*)-$", wordlist[i1][0])
            if m and m.group(1) + wordlist[i1+1][0] == wl_new[j1][0]:
              if debug:
                print "not strict: ign '%s','%s' -> '%s'" % (wordlist[i1][0],wordlist[i1+1][0],wl_new[j1][0])
              continue      # "foo-", "bar" became "foobar"
          if i_len < j_len:
            # print("getting longer by %d" % (j_len-i_len))
            yield ('replace',  i1,i2, j1,j1+i_len)
            yield ('insert',   i2,i2, j1+i_len,j2)
          elif i_len > j_len:
            # print("getting shorter by %d" % (i_len-j_len))
            yield ('replace', i1,i1+j_len, j1, j2)
            yield ('delete',  i1+j_len,i2, j2, j2)
          else:
            # same length
            yield (tag, i1, i2, j1, j2)
        else:
          yield (tag, i1, i2, j1, j2)

    for tag, i1, i2, j1, j2 in opcodes_post_proc(s.get_opcodes()):
      if tag == "equal":
        if 'e' in ops:
          attr = ext['e'].copy()
          # no need to put the old text into attr['o'], it is unchanged.
          attr['t'] = 'equ'
        else:
          continue
      elif tag == "replace":
        if 'c' in ops:
          attr = ext['c'].copy()
          attr['o'] = catwords(wordlist, i1, i2)
          attr['t'] = 'chg'
        else:
          continue
      elif tag == "delete":
        if 'd' in ops:
          attr = ext['d'].copy()
          attr['o'] = catwords(wordlist, i1, i2)
          attr['t'] = 'del'
          j2 = j1 + 1     # so that the create_mark loop below executes once.
        else:
          continue
      elif tag == "insert":
        if 'a' in ops:
          attr = ext['a'].copy()
          attr['t'] = 'add'
        else:
          continue
      else:
        print("SequenceMatcher returned unknown tag: %s" % tag)
        continue
      # print("len(wl_new)=%d, j in [%d:%d] %s" % (len(wl_new), j1, j2,tag))
      for j in range(j1,j2):
        if j >= len(wl_new):    # this happens with autojunk=False!
          print("end of wordlist reached: %d" % j)
          break
        markword(p_rect_dict, wl_new, j, attr, fontinfo)

  if spell_check:
    h = Hunspell()
    word_set = set()
    for word in wl_new:
      word_set.add(word[0])
    bad_word_dict = dict()
    print("%d words to check" % len(word_set))
    for word in word_set:
      r = h.check(word)
      if r is not None:
        bad_word_dict[word] = r
    print("checked: %d bad" % len(bad_word_dict))
      
    idx = 0
    for word in wl_new:
      result = h.check(word[0])
      if word[0] in bad_word_dict:
        attr = ext['e'].copy()
        attr['o'] = ", ".join(bad_word_dict[word[0]])
        attr['t'] = "spl"
        markword(p_rect_dict, wl_new, idx, attr, fontinfo)
      idx += 1

  # End of wordlist code.
  # We have now p_rect_dict preloaded with the wordlist marks or empty.
  # next loop through all pages, select the correct p_rect from the dict.
  # Start or continue adding re_pattern search results while if any.
  # Finally collect all in pages_a.in pages_a.
  pages_a = []
  p_nr = 0
  for p in dom.findall('page'):
    if not last_page is None:
      if p_nr > int(last_page):
        break
    p_nr += 1

    p_rect = p_rect_dict.get(p_nr,[])
    if re_pattern:
      for e in p.findall('text'):
        p_finfo = fontinfo[p_nr]
        text = ''
        for t in e.itertext(): text += t
        if not strict:
          text = zap_letter_spacing(text)
  
        #pprint([e.attrib, text])
        #print("search (%s)" % re_pattern)
        flags = re.UNICODE
        if (nocase): flags |= re.IGNORECASE
        l = map(lambda x:len(x), re.split('('+re_pattern+')', text, flags=flags))
        l.append(0)       # dummy to make an even number.
        # all odd indices in l are word lengths, all even ones are seperator lengths
        offset = 0
        i = 0
        while (i < len(l)):
          # print("offset=%d, i=%d, l=%s" % (offset, i, repr(l)))
          offset += l[i]
          if (l[i+1] > 0):
  
            p_rect.append(create_mark(text,offset,l[i+1], 
              p_finfo[e.attrib['font']]['font'], 
              e.attrib['left'], e.attrib['top'], 
              e.attrib['width'],e.attrib['height'], ext['e']))
    
            offset += l[i+1]
          i += 2
    pages_a.append({'nr':int(p.attrib['number']), 'rect':p_rect, 
                 'nav_c':ext['e'].get('c',[.5,.5,.5]),
                 'h':float(p.attrib['height']), 'w':float(p.attrib['width']),
                 'x':float(p.attrib['left']), 'y':float(p.attrib['top'])})
  return pages_a


class Hunspell():
    def __init__(self):
        self.cmd = ['hunspell', '-i', 'utf-8']
        try:
          self.proc = subprocess.Popen(self.cmd, shell=False, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        except OSError as e:
          self.proc = "%s failed: errno=%d %s" % (self.cmd, e.errno, e.strerror)
          raise OSError(self.proc)
        self.version = self.proc.stdout.readline().rstrip()
        
    def check(self, word):
        # It is horrible. The words already know that they contain utf8-glyphs,
        # the error message says it: 'ascii' codec can't encode character u'\xdc'.
        # But write still just allows 7-bit ascii, unless I say encode('utf8') before 
        # calling write. How does it know the difference? 
        self.proc.stdin.write((word+"\n").encode('utf8'))
        while True:
            output = self.proc.stdout.readline().rstrip()
            if len(output): break
        if output == '*': return None
        if output[0] != '&': return output
        a = output.split(': ')
        return a[1].split(', ')

if __name__ == "__main__": main()

