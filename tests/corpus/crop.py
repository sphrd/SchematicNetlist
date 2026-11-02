import pypdfium2 as pdfium, sys
doc = pdfium.PdfDocument(sys.argv[1]); p = doc[int(sys.argv[2])-1]
x0,y0,x1,y1 = map(float, sys.argv[3].split(","))
img = p.render(scale=float(sys.argv[4])).to_pil()
s=float(sys.argv[4]); img.crop((int(x0*s),int(y0*s),int(x1*s),int(y1*s))).save(sys.argv[5])
