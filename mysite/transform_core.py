import os
import configparser
cf = configparser.ConfigParser()
cf.read("project.config")
Debug = cf.get("backend", "Debug")=="True"
if not Debug:
    import allel
import numpy as np
import json
import multiprocessing
import pickle
import re
import zipfile
from wsgiref.util import FileWrapper
from functools import partial
import time
import sys
import gzip
import copy
import vcf
if sys.platform.startswith('linux'):
    import fcntl
else:
    from lockfile import LockFile


class MyEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (np.int_, np.intc, np.intp, np.int8,
                            np.int16, np.int32, np.int64, np.uint8,
                            np.uint16, np.uint32, np.uint64)):
            return int(obj)
        elif isinstance(obj, (np.float_, np.float16, np.float32,
                              np.float64)):
            return float(obj)
        elif isinstance(obj, np.ndarray):
            return list(obj)
        elif isinstance(obj, np.bool_):
            return bool(obj)
        else:
            return json.JSONEncoder.default(self, obj)

count = 0
def CountLoop(bulk=1000000):
    global count
    count += 1
    if count % bulk == 0:
        print("run data num: ", count)

def RenameJsonKey(strJson):
    if isinstance(strJson,dict):
        strJson = json.dumps(strJson)
    #先默认json的key中没有特殊符号
    pattern = re.compile(r"\"([\w.$:]+)\":")
    strJson = pattern.sub(lambda m: m.group(0).replace('.', "_").replace('$', "^"), strJson)
    return strJson


class TransformV2J(object):
    def addhead(self, header, filepath_json):
        record_head = []
        for line in header:
            line = line.strip('\n')
            record_head.append(line)
        record = {
           "Header": record_head
        }
        with open(filepath_json, 'a') as fp:
            recordstring = json.dumps(record, cls=MyEncoder)
            recordstring = recordstring[:-1] + ',' + '\n'
            fp.write(recordstring)
            fp.write('"Data":[')
        return

    #delete the last comma, and add the bracket
    def addEnd(self, filepath_json):
        # in win platform is \r\n; in linux is \n; in os is \r
        if sys.platform.startswith('win'):
            offsize = -3
        else:
            offsize = -2
        with open(filepath_json, 'rb+') as filehandle:
            #delete \n and ,
            filehandle.seek(offsize, os.SEEK_END)
            filehandle.truncate()
        with open(filepath_json, 'a') as fp:
            fp.write(']}')

    def chunker2string(self, chunker, fields, samples, mode='MergeSamples'):
        li = []
        # 把NaN转换成-1
        for i in range(chunker[1]):
            for field in fields:
                if isinstance(chunker[0][field][i], np.ndarray) and not isinstance(chunker[0][field][i][0], np.str):
                    nanpos = np.isnan(chunker[0][field][i])
                    chunker[0][field][i][nanpos] = -1.0

        if mode == 'MergeAll':
            for i in range(chunker[1]):
                #basic
                recorddict1 = {
                    "CHROM": chunker[0]['variants/CHROM'][i],
                    "POS" : chunker[0]['variants/POS'][i],
                    "ID": chunker[0]['variants/ID'][i],
                    "REF": chunker[0]['variants/REF'][i],
                    "ALT": chunker[0]['variants/ALT'][i],
                    "QUAL": chunker[0]['variants/QUAL'][i],
                }
                #filter
                recorddict2 = {
                    "FILTER": {
                        k_filter[9:] : chunker[0][k_filter][i] for k_filter in fields if 'variants/FILTER' in k_filter
                    }
                }
                #Info
                recorddict3 = {
                    "INFO": {
                        k_Info[9:] : chunker[0][k_Info][i] for k_Info in fields if k_Info not in ['variants/CHROM', 'variants/POS', 'variants/ID', 'variants/REF', 'variants/ALT', 'variants/QUAL', 'variants/numalt', 'variants/svlen', 'variants/is_snp']
                        and 'variants/FILTER' not in k_Info and 'calldata/' not in k_Info
                    }
                }
                #Samples
                recordsamples = []
                for k_sample, j in zip(samples, range(samples.size)):
                    recordsample1 = {
                        "SAMPLENO": k_sample
                    }
                    recordsample2 = {
                        k_field[9:]: [chunker[0][k_field][i][j][n] for n in
                                      range(chunker[0][k_field][i][j].size)] if isinstance(
                            chunker[0][k_field][i][j], np.ndarray) else chunker[0][k_field][i][j] for k_field in
                        fields if "calldata/" in k_field
                    }
                    recordsample = dict(recordsample1, **recordsample2)
                    recordsamples.append(recordsample)
                recorddict4 = {
                    "SAMPLES": recordsamples
                }
                recorddictMerge = dict(recorddict1, **recorddict2, **recorddict3, **recorddict4)
                li.append(recorddictMerge)

        elif mode == 'MergeSamples':
            for i in range(chunker[1]):
                recorddict1 = {
                    k_field[9:]: [chunker[0][k_field][i][m] for m in range(chunker[0][k_field][i].size)] if isinstance(
                        chunker[0][k_field][i], np.ndarray) else chunker[0][k_field][i] for k_field in fields if
                    'variants/' in k_field and k_field not in  ['variants/numalt', 'variants/svlen', 'variants/is_snp']
                }
                recordsamples = []
                for k_sample, j in zip(samples, range(samples.size)):
                    recordsample1 = {
                        "SAMPLENO": k_sample
                    }
                    recordsample2 = {
                        k_field[9:]: [chunker[0][k_field][i][j][n] for n in
                                      range(chunker[0][k_field][i][j].size)] if isinstance(
                            chunker[0][k_field][i][j], np.ndarray) else chunker[0][k_field][i][j] for k_field in
                        fields if "calldata/" in k_field
                    }
                    recordsample = dict(recordsample1, **recordsample2)
                    recordsamples.append(recordsample)
                recorddict2 = {
                    "SAMPLES": recordsamples
                }

                recorddict = dict(recorddict1, **recorddict2)
                li.append(recorddict)

        recordstring = json.dumps(li, cls=MyEncoder)
        recordstring = recordstring[1:-1]   #delete first and last brackets.  "[...]" ----> "..."
        recordstring = recordstring + ','+ '\n'
        return recordstring


    def IoOperat_multi(self, tmpfile, mode, chunker):
        # tmpfile = "value_" + md5 + ".dat"
        with open(tmpfile, "rb") as f:
            fields = pickle.load(f)
            samples = pickle.load(f)
            headers = pickle.load(f)
            filepath_json = pickle.load(f)
        recordstring = self.chunker2string(chunker, fields, samples, mode)
        if sys.platform.startswith('linux'):
            with open(filepath_json, "a") as fp:
                fcntl.flock(fp.fileno(), fcntl.LOCK_EX)
                fp.write(recordstring)
        else:
            lock = LockFile(filepath_json)
            lock.acquire()
            with open(filepath_json, "a") as fp:
                fp.write(recordstring)
            lock.release()
        return

    #useless function. just for test
    def vcf2json_Single(self, filepath_vcf, filepath_json, mode):
        fields, samples, headers, chunks = allel.iter_vcf_chunks(filepath_vcf, fields=['*'], chunk_length=50)

        if os.path.exists(filepath_json):
            os.remove(filepath_json)
        self.addhead(headers[0], filepath_json)

        for chunker in chunks:
            with open(filepath_json, 'a') as fp:
                recordstring = self.chunker2string(chunker, fields, samples, mode)
                fp.write(recordstring)

        return

    def vcf2json_multi2(self, filepath_vcf, filepath_json, md5, mode, IsAddHead= True):
        fields, samples, headers, chunks = allel.iter_vcf_chunks(filepath_vcf, fields=['variants/*', 'calldata/*'],chunk_length=500)

        if os.path.exists(filepath_json):
            os.remove(filepath_json)
        #增加原vcf文件的头部信息, 用于逆向转换
        if IsAddHead:
            self.addhead(headers[0], filepath_json)

        tmpfile = "value_" + md5 + ".dat"
        with open(tmpfile, "wb") as f:
            pickle.dump(fields, f)
            pickle.dump(samples, f)
            pickle.dump(headers, f)
            pickle.dump(filepath_json, f)

        cores = multiprocessing.cpu_count()
        processnum = int(cores / 2)
        if processnum < 2:
            processnum = 2
        # 自己调度迭代器 防止内存溢出
        pool = multiprocessing.Pool(processes=processnum)
        index = 0
        tmpchunks = []
        first = True
        realchunks = []
        for chunker in chunks:
            index += 1
            tmpchunks.append(chunker)
            if index % (processnum * 10) == 0:
                if not first:
                    AppResult.get()
                    realchunks.clear()
                realchunks = copy.deepcopy(tmpchunks)
                tmpchunks.clear()
                first = False
                AppResult = pool.map_async(partial(self.IoOperat_multi, tmpfile, mode), realchunks)

        if "AppResult" in locals().keys():
            AppResult.get()

        pool.map(partial(self.IoOperat_multi, tmpfile, mode), tmpchunks)
        tmpchunks.clear()
        if realchunks:
            realchunks.clear()
        pool.close()
        pool.join()  # 主进程阻塞等待子进程的退出
        #delete two last character '\n' and ',' and add '}'
        if IsAddHead:
            self.addEnd(filepath_json)
        os.remove(tmpfile)  # 删除临时文件,节约空间


    def dotranform(self, filepath_vcf, mode, IsAddHead):
        file_json = os.path.splitext(filepath_vcf)[0] + ".json"
        self.vcf2json_multi2(filepath_vcf, file_json, "tmpdat", mode, IsAddHead)


    #with output path
    def dotransformWithOutPath(self, filepath_vcf, filepath_json, mode, IsAddHead):
        self.vcf2json_multi2(filepath_vcf, filepath_json, "tmpdat", mode, IsAddHead)


    def preview(self, filepath_vcf, mode):
        fields, samples, headers, chunks = allel.iter_vcf_chunks(filepath_vcf, fields=['*'], chunk_length=2)
        #get first 2 lines for example
        #get json
        for chunker in chunks:
            recordstring = self.chunker2string(chunker, fields, samples, mode)
            recordstring = RenameJsonKey(recordstring)
            break

        #get vcf
        if filepath_vcf.endswith('gz'): #.vcf.gz
            linenum = 0
            vcfline = str()
            with gzip.open(filepath_vcf, 'rb') as file:
                for line in file:
                    if not line:
                        break
                    else:
                        strline = bytes.decode(line)
                        if strline[1] != '#':
                            vcfline += strline
                            linenum += 1
                            if linenum == 3:
                                break
            result = {"vcf": vcfline, "json": recordstring}
        else:   #.vcf
            linenum = 0
            vcfline = str()
            with open(filepath_vcf, 'rb') as file:
                while True:
                    line = file.readline()
                    if not line:
                        break
                    else:
                        if line[1] != '#':
                            vcfline += line
                            linenum += 1
                            if linenum == 3:
                                break

            result = {"vcf": vcfline, "json": recordstring}
        return result

    def GetVCFHeader(self, filepath_vcf):
        print("begin read header")
        headers = allel.read_vcf_headers(filepath_vcf)
        print("read header complete")
        infofields = []
        ontologyfield = ['HPO', 'DO', 'SO', 'MC', 'GO', ]
        i = 0
        for ontoele in ontologyfield:
            if ontoele in headers.infos:
                i += 1
        if i == len(ontologyfield):  # has ontology, these ontology should be put behind
            for ele in headers.infos:
                if ele not in ontologyfield:
                    infofields.append(ele)
            infofields.extend(ontologyfield)
        else:
            for ele in headers.infos:
                infofields.append(ele)
        return infofields

    # because of lib of scikit-allel has bug on clinvar data, regard this function as main transform
    def TransformMain(self, input, output):
        def WriteFile(list):
            recordstring = json.dumps(list, cls=MyEncoder)
            recordstring = recordstring[1:-1]  # delete first and last brackets.  "[...]" ----> "..."
            recordstring = recordstring + ',' + '\n'
            with open(output, 'a') as fp:
                fp.write(recordstring)
        #clear file
        with open(output, 'w') as f:
            pass
        vcf_reader = vcf.Reader(open(input, 'r'))
        li = []
        count = 0
        for record in vcf_reader:
            CountLoop()
            # basic
            recorddict1 = {
                "CHROM": record.CHROM if record.CHROM != None else '.',
                "POS": record.POS if record.POS != None else '.',
                "ID": record.ID if record.ID != None else '.',
                "REF": record.REF if record.REF != None else '.',
                "ALT": str(record.ALT)[1:-1] if str(record.ALT)[1:-1] != 'None' else '.',
                "QUAL": record.QUAL if record.QUAL != None else '.',
                "FILTER": record.FILTER if record.FILTER != None else '.'
            }
            # info
            recorddict2 = {
                "INFO": {
                    k_info: record.INFO[k_info] if 'None' not in str(record.INFO[k_info]) else '.' for k_info in
                    record.INFO
                }
            }
            # Samples
            if record.samples != None:
                recordsamples = []
                for k_sample in record.samples:
                    recordsample1 = {
                        "SAMPLENO": k_sample.sample
                    }
                    recordsample2 = {}
                    for k_smple_d, i in zip(k_sample.data._fields, range(len(k_sample.data._fields))):
                        if isinstance(k_sample.data[i], list):
                            tmplist = ['.' if j == None else j for j in k_sample.data[i]]
                            recordsample2[k_smple_d] = tmplist
                        else:
                            if k_sample.data[i] == None:
                                recordsample2[k_smple_d] = '.'
                            else:
                                recordsample2[k_smple_d] = k_sample.data[i]

                    recordsample = dict(recordsample1, **recordsample2)
                    recordsamples.append(recordsample)
                recorddict3 = {
                    "SAMPLES": recordsamples
                }
                recorddictMerge = dict(recorddict1, **recorddict2, **recorddict3)
            else:
                recorddictMerge = dict(recorddict1, **recorddict2)
            li.append(recorddictMerge)
            count += 1
            if count == 50:
                WriteFile(li)
                count = 0
                li = []
        WriteFile(li)
        print("TransformMain done!")