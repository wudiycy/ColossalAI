import argparse
import os


def det_cor(loc_li):
    for loc in loc_li:
        real_loc = 'examples/'+eval(loc)
        if not os.path.exists(real_loc):
            return -1
    return 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--fileNameList', type=str)
    args = parser.parse_args()
    name_list = args.fileNameList.split(",")
    result = det_cor(name_list)
    print(result)




if __name__ == '__main__':
    main()
